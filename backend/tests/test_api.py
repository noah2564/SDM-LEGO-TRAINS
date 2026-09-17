"""REST API, persistence and emergency-stop behaviour."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app import repository
from app.db import Database
from app.train_service import TrainService

from .conftest import FakeBroker


class TestTrainCrud:
    async def test_empty_fleet(self, client: AsyncClient) -> None:
        response = await client.get("/api/trains")
        assert response.status_code == 200
        assert response.json() == []

    async def test_create_and_fetch(self, client: AsyncClient, train: dict[str, Any]) -> None:
        assert train["id"] == "train-001"
        assert train["status"] == "unknown"
        assert train["speed"] == 0

        response = await client.get("/api/trains/train-001")
        assert response.status_code == 200
        assert response.json()["name"] == "ICE"

    async def test_duplicate_id_conflicts(
        self, client: AsyncClient, train: dict[str, Any]
    ) -> None:
        response = await client.post(
            "/api/trains",
            json={"id": "train-001", "name": "Copy", "device_id": "pico-999"},
        )
        assert response.status_code == 409

    async def test_duplicate_device_conflicts(
        self, client: AsyncClient, train: dict[str, Any]
    ) -> None:
        response = await client.post(
            "/api/trains",
            json={"id": "train-002", "name": "Freight", "device_id": "pico-001"},
        )
        assert response.status_code == 409

    @pytest.mark.parametrize("train_id", ["train/001", "train+001", "a", "train#1"])
    async def test_invalid_ids_rejected(self, client: AsyncClient, train_id: str) -> None:
        response = await client.post(
            "/api/trains", json={"id": train_id, "name": "X", "device_id": "pico-x"}
        )
        assert response.status_code == 422

    async def test_update(self, client: AsyncClient, train: dict[str, Any]) -> None:
        response = await client.put(
            "/api/trains/train-001", json={"name": "ICE 3", "max_speed": 60}
        )
        assert response.status_code == 200
        assert response.json()["name"] == "ICE 3"
        assert response.json()["max_speed"] == 60

    async def test_delete(self, client: AsyncClient, train: dict[str, Any]) -> None:
        assert (await client.delete("/api/trains/train-001")).status_code == 204
        assert (await client.get("/api/trains/train-001")).status_code == 404

    async def test_missing_train_is_404(self, client: AsyncClient) -> None:
        assert (await client.get("/api/trains/nope")).status_code == 404


class TestPersistence:
    async def test_state_survives_a_new_connection(
        self, client: AsyncClient, train: dict[str, Any], service: TrainService, database: Database
    ) -> None:
        await service.handle_message(
            "trains/train-001/telemetry",
            b'{"speed": 42, "direction": "backward", "battery": 7.1}',
        )
        # Re-read through a brand new session, as a restarted process would.
        async with database.session() as session:
            stored = await repository.get_train(session, "train-001")
            assert stored is not None
            assert stored.speed == 42
            assert stored.direction == "backward"
            assert stored.battery == 7.1
            assert stored.status == "online"

    async def test_events_are_persisted(
        self, client: AsyncClient, train: dict[str, Any], service: TrainService
    ) -> None:
        await service.handle_message("trains/train-001/status", b'{"status": "online"}')
        response = await client.get("/api/trains/train-001/events")
        assert response.status_code == 200
        types = [event["type"] for event in response.json()]
        assert "status_change" in types

    async def test_event_log_is_pruned(
        self, client: AsyncClient, train: dict[str, Any], service: TrainService
    ) -> None:
        service.settings.max_events_per_train = 5
        for index in range(12):
            await service.handle_message(
                "trains/train-001/event", f'{{"type": "test", "message": "{index}"}}'.encode()
            )
        response = await client.get("/api/trains/train-001/events", params={"limit": 100})
        assert len(response.json()) == 5


class TestCommands:
    async def test_set_speed_reaches_the_broker(
        self, client: AsyncClient, train: dict[str, Any], broker: FakeBroker
    ) -> None:
        response = await client.post(
            "/api/trains/train-001/command", json={"command": "set_speed", "speed": 45}
        )
        assert response.status_code == 200
        published = broker.commands_for("train-001")
        assert published[-1]["command"] == "set_speed"
        assert published[-1]["speed"] == 45
        assert (await client.get("/api/trains/train-001")).json()["speed"] == 45

    async def test_invalid_command_never_reaches_the_broker(
        self, client: AsyncClient, train: dict[str, Any], broker: FakeBroker
    ) -> None:
        response = await client.post(
            "/api/trains/train-001/command", json={"command": "set_speed", "speed": 900}
        )
        assert response.status_code == 422
        assert broker.commands_for("train-001") == []

    async def test_speed_above_train_limit_is_rejected(
        self, client: AsyncClient, train: dict[str, Any], broker: FakeBroker
    ) -> None:
        await client.put("/api/trains/train-001", json={"max_speed": 50})
        response = await client.post(
            "/api/trains/train-001/command", json={"command": "set_speed", "speed": 80}
        )
        assert response.status_code == 422
        assert broker.commands_for("train-001") == []

    async def test_stop_endpoint(
        self, client: AsyncClient, train: dict[str, Any], broker: FakeBroker
    ) -> None:
        await client.post("/api/trains/train-001/command", json={"command": "set_speed", "speed": 60})
        response = await client.post("/api/trains/train-001/stop")
        assert response.status_code == 200
        assert broker.commands_for("train-001")[-1]["command"] == "stop"
        assert (await client.get("/api/trains/train-001")).json()["speed"] == 0

    async def test_command_for_unknown_train_is_404(self, client: AsyncClient) -> None:
        response = await client.post("/api/trains/ghost/stop")
        assert response.status_code == 404

    async def test_broker_outage_is_reported(
        self, client: AsyncClient, train: dict[str, Any], broker: FakeBroker
    ) -> None:
        broker.connected = False
        response = await client.post("/api/trains/train-001/stop")
        assert response.status_code == 409
        assert "broker" in response.json()["detail"].lower()


class TestEmergencyStop:
    async def test_emergency_stop_latches_and_blocks_driving(
        self, client: AsyncClient, train: dict[str, Any], broker: FakeBroker
    ) -> None:
        response = await client.post(
            "/api/trains/train-001/emergency-stop", json={"reason": "derailment"}
        )
        assert response.status_code == 200
        sent = broker.commands_for("train-001")[-1]
        assert sent["command"] == "emergency_stop"
        assert sent["reason"] == "derailment"

        state = (await client.get("/api/trains/train-001")).json()
        assert state["emergency_stop"] is True
        assert state["speed"] == 0

        blocked = await client.post(
            "/api/trains/train-001/command", json={"command": "set_speed", "speed": 30}
        )
        assert blocked.status_code == 409

    async def test_stop_still_works_while_latched(
        self, client: AsyncClient, train: dict[str, Any]
    ) -> None:
        await client.post("/api/trains/train-001/emergency-stop")
        assert (await client.post("/api/trains/train-001/stop")).status_code == 200

    async def test_clearing_the_latch_restores_control(
        self, client: AsyncClient, train: dict[str, Any]
    ) -> None:
        await client.post("/api/trains/train-001/emergency-stop")
        assert (await client.post("/api/trains/train-001/clear-emergency")).status_code == 200
        assert (await client.get("/api/trains/train-001")).json()["emergency_stop"] is False
        assert (
            await client.post(
                "/api/trains/train-001/command", json={"command": "set_speed", "speed": 30}
            )
        ).status_code == 200

    async def test_global_stop_hits_every_train_and_the_broadcast_topic(
        self, client: AsyncClient, train: dict[str, Any], broker: FakeBroker
    ) -> None:
        await client.post(
            "/api/trains", json={"id": "train-002", "name": "Freight", "device_id": "pico-002"}
        )
        response = await client.post("/api/emergency-stop", json={"reason": "all stop"})
        assert response.status_code == 200
        assert sorted(response.json()["trains"]) == ["train-001", "train-002"]
        assert "system/command" in broker.topics()
        assert broker.commands_for("train-001")[-1]["command"] == "emergency_stop"
        assert broker.commands_for("train-002")[-1]["command"] == "emergency_stop"

        for train_id in ("train-001", "train-002"):
            assert (await client.get(f"/api/trains/{train_id}")).json()["emergency_stop"] is True


class TestSystemEndpoints:
    async def test_health(self, client: AsyncClient) -> None:
        response = await client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] is True

    async def test_stats_counts_states(
        self, client: AsyncClient, train: dict[str, Any], service: TrainService
    ) -> None:
        await service.handle_message(
            "trains/train-001/telemetry", b'{"speed": 20, "direction": "forward"}'
        )
        stats = (await client.get("/api/stats")).json()
        assert stats["total"] == 1
        assert stats["online"] == 1
        assert stats["moving"] == 1

    async def test_unregistered_trains_are_listed_for_discovery(
        self, client: AsyncClient, service: TrainService
    ) -> None:
        await service.handle_message(
            "trains/train-042/status", b'{"status": "online", "device_id": "pico-042"}'
        )
        discovered = (await client.get("/api/discovered")).json()
        assert discovered[0]["train_id"] == "train-042"
        assert discovered[0]["device_id"] == "pico-042"
