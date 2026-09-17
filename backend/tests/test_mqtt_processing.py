"""MQTT ingest, LWT handling and online/offline state management."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from httpx import AsyncClient

from app import repository
from app.db import Database
from app.train_service import TrainService


class TestIngest:
    async def test_status_online_is_applied(
        self, service: TrainService, train: dict[str, Any], client: AsyncClient
    ) -> None:
        await service.handle_message(
            "trains/train-001/status", b'{"status": "online", "device_id": "pico-001"}'
        )
        state = (await client.get("/api/trains/train-001")).json()
        assert state["status"] == "online"
        assert state["last_seen"] is not None

    async def test_last_will_marks_train_offline_and_zeroes_speed(
        self, service: TrainService, train: dict[str, Any], client: AsyncClient
    ) -> None:
        await service.handle_message(
            "trains/train-001/telemetry", b'{"speed": 70, "direction": "forward"}'
        )
        assert (await client.get("/api/trains/train-001")).json()["speed"] == 70

        # This is exactly what Mosquitto publishes on an unexpected disconnect.
        await service.handle_message(
            "trains/train-001/status", b'{"status": "offline", "reason": "lwt"}'
        )
        state = (await client.get("/api/trains/train-001")).json()
        assert state["status"] == "offline"
        assert state["speed"] == 0

    async def test_telemetry_alone_brings_a_train_online(
        self, service: TrainService, train: dict[str, Any], client: AsyncClient
    ) -> None:
        await service.handle_message("trains/train-001/telemetry", b'{"speed": 0}')
        assert (await client.get("/api/trains/train-001")).json()["status"] == "online"

    async def test_device_reported_emergency_stop_is_mirrored(
        self, service: TrainService, train: dict[str, Any], client: AsyncClient
    ) -> None:
        await service.handle_message(
            "trains/train-001/telemetry", b'{"speed": 0, "emergency_stop": true}'
        )
        assert (await client.get("/api/trains/train-001")).json()["emergency_stop"] is True

    async def test_error_event_is_recorded(
        self, service: TrainService, train: dict[str, Any], client: AsyncClient
    ) -> None:
        await service.handle_message(
            "trains/train-001/event",
            b'{"type": "hub_error", "message": "hub link lost", "severity": "error"}',
        )
        state = (await client.get("/api/trains/train-001")).json()
        assert state["last_error"] == "hub link lost"
        events = (await client.get("/api/trains/train-001/events")).json()
        assert events[0]["type"] == "hub_error"

    async def test_malformed_payload_is_ignored(
        self, service: TrainService, train: dict[str, Any], client: AsyncClient
    ) -> None:
        await service.handle_message("trains/train-001/telemetry", b"not json at all")
        await service.handle_message("trains/train-001/telemetry", b"[1,2,3]")
        assert (await client.get("/api/trains/train-001")).json()["status"] == "unknown"

    async def test_unrelated_topic_is_ignored(self, service: TrainService) -> None:
        await service.handle_message("system/heartbeat", b"{}")  # must not raise

    async def test_hostile_telemetry_cannot_exceed_limits(
        self, service: TrainService, train: dict[str, Any], client: AsyncClient
    ) -> None:
        await service.handle_message(
            "trains/train-001/telemetry",
            b'{"speed": 99999, "battery": -5, "direction": "drop table"}',
        )
        state = (await client.get("/api/trains/train-001")).json()
        assert state["speed"] == 100
        assert state["battery"] == 0.0
        assert state["direction"] == "forward"


class TestStalenessMonitor:
    async def test_silent_train_is_downgraded_then_marked_offline(
        self, service: TrainService, database: Database, train: dict[str, Any]
    ) -> None:
        await service.handle_message("trains/train-001/status", b'{"status": "online"}')

        async with database.session() as session:
            stored = await repository.get_train(session, "train-001")
            assert stored is not None
            stored.last_seen = datetime.now(timezone.utc) - timedelta(seconds=30)

        await service.start_background_tasks()
        try:
            await asyncio.sleep(0.3)
            async with database.session() as session:
                stored = await repository.get_train(session, "train-001")
                assert stored is not None
                # 30s of silence is past both the 1s and the 3s thresholds.
                assert stored.status == "offline"
                assert stored.speed == 0
        finally:
            await service.stop_background_tasks()

    async def test_heartbeat_is_published_for_the_fleet(
        self, service: TrainService, broker: Any
    ) -> None:
        await service.start_background_tasks()
        try:
            await asyncio.sleep(0.25)
        finally:
            await service.stop_background_tasks()
        assert "system/heartbeat" in broker.topics()
