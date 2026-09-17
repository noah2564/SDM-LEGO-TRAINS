"""Integration test: simulated Pico -> MQTT -> backend -> WebSocket/API.

The FakeBroker moves messages both ways, so this exercises the real ingest
path, the real service logic, the real REST layer and the real WebSocket hub
in a single flow. A companion test that uses an actual Mosquitto broker lives
in test_broker_integration.py.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import AsyncClient

from app import protocol
from app.train_service import TrainService

from .conftest import FakeBroker


class SimulatedPico:
    """A minimal device: subscribes to its command topic, reports telemetry."""

    def __init__(self, broker: FakeBroker, service: TrainService, train_id: str, device_id: str):
        self.broker = broker
        self.service = service
        self.train_id = train_id
        self.device_id = device_id
        self.speed = 0
        self.direction = "forward"
        self.emergency_stop = False
        self.received: list[dict[str, Any]] = []
        self.inbox = broker.subscribe(protocol.command_topic(train_id))

    async def connect(self) -> None:
        await self.service.handle_message(
            protocol.status_topic(self.train_id),
            json.dumps(
                {"status": "online", "device_id": self.device_id, "firmware": "sim-1.0"}
            ).encode(),
        )

    async def disconnect_uncleanly(self) -> None:
        """What Mosquitto does on behalf of a device that vanished."""
        await self.service.handle_message(
            protocol.status_topic(self.train_id),
            json.dumps(protocol.offline_will_payload(self.train_id, self.device_id)).encode(),
        )

    async def publish_telemetry(self) -> None:
        await self.service.handle_message(
            protocol.telemetry_topic(self.train_id),
            json.dumps(
                {
                    "train_id": self.train_id,
                    "timestamp": protocol.utcnow_iso(),
                    "speed": self.speed,
                    "direction": self.direction,
                    "battery": 7.4,
                    "connected": True,
                    "emergency_stop": self.emergency_stop,
                }
            ).encode(),
        )

    async def pump(self, timeout: float = 1.0) -> dict[str, Any]:
        """Wait for one command, apply it, and report back."""
        command = await asyncio.wait_for(self.inbox.get(), timeout=timeout)
        self.received.append(command)
        name = command["command"]
        if name == "set_speed" and not self.emergency_stop:
            self.speed = command["speed"]
            self.direction = command.get("direction", self.direction)
        elif name == "set_direction":
            self.direction = command["direction"]
        elif name == "stop":
            self.speed = 0
        elif name == "emergency_stop":
            self.speed = 0
            self.emergency_stop = True
        elif name == "clear_emergency":
            self.emergency_stop = False
        await self.publish_telemetry()
        return command


@pytest.fixture
def pico(broker: FakeBroker, service: TrainService) -> SimulatedPico:
    return SimulatedPico(broker, service, "train-001", "pico-001")


class TestFullLoop:
    async def test_device_appears_online_and_drives(
        self,
        client: AsyncClient,
        train: dict[str, Any],
        pico: SimulatedPico,
    ) -> None:
        await pico.connect()
        assert (await client.get("/api/trains/train-001")).json()["status"] == "online"

        # UI -> API -> validation -> MQTT -> device
        response = await client.post(
            "/api/trains/train-001/command",
            json={"command": "set_speed", "speed": 45, "direction": "forward"},
        )
        assert response.status_code == 200
        command = await pico.pump()
        assert command["command"] == "set_speed"
        assert pico.speed == 45

        # device telemetry -> MQTT -> backend -> API
        state = (await client.get("/api/trains/train-001")).json()
        assert state["speed"] == 45
        assert state["last_telemetry"]["battery"] == 7.4

    async def test_direction_change_and_stop(
        self, client: AsyncClient, train: dict[str, Any], pico: SimulatedPico
    ) -> None:
        await pico.connect()
        await client.post(
            "/api/trains/train-001/command", json={"command": "set_speed", "speed": 30}
        )
        await pico.pump()
        await client.post(
            "/api/trains/train-001/command",
            json={"command": "set_direction", "direction": "backward"},
        )
        await pico.pump()
        assert pico.direction == "backward"

        await client.post("/api/trains/train-001/stop")
        await pico.pump()
        assert pico.speed == 0
        assert (await client.get("/api/trains/train-001")).json()["speed"] == 0

    async def test_emergency_stop_reaches_the_device(
        self, client: AsyncClient, train: dict[str, Any], pico: SimulatedPico
    ) -> None:
        await pico.connect()
        await client.post(
            "/api/trains/train-001/command", json={"command": "set_speed", "speed": 70}
        )
        await pico.pump()

        await client.post("/api/emergency-stop", json={"reason": "test"})
        command = await pico.pump()
        assert command["command"] == "emergency_stop"
        assert pico.speed == 0
        assert pico.emergency_stop is True
        assert (await client.get("/api/trains/train-001")).json()["emergency_stop"] is True

    async def test_disconnect_transitions_to_offline(
        self, client: AsyncClient, train: dict[str, Any], pico: SimulatedPico
    ) -> None:
        await pico.connect()
        await pico.publish_telemetry()
        assert (await client.get("/api/trains/train-001")).json()["status"] == "online"

        await pico.disconnect_uncleanly()
        assert (await client.get("/api/trains/train-001")).json()["status"] == "offline"

    async def test_two_trains_are_controlled_independently(
        self, client: AsyncClient, train: dict[str, Any], broker: FakeBroker, service: TrainService
    ) -> None:
        await client.post(
            "/api/trains", json={"id": "train-002", "name": "Freight", "device_id": "pico-002"}
        )
        first = SimulatedPico(broker, service, "train-001", "pico-001")
        second = SimulatedPico(broker, service, "train-002", "pico-002")
        await first.connect()
        await second.connect()

        await client.post(
            "/api/trains/train-001/command", json={"command": "set_speed", "speed": 20}
        )
        await first.pump()
        assert first.speed == 20
        assert second.speed == 0
        assert second.inbox.empty()


def test_websocket_receives_live_updates(app: FastAPI, service: TrainService) -> None:
    """Telemetry arriving on MQTT must reach the browser without polling."""
    with TestClient(app) as client:
        created = client.post(
            "/api/trains",
            json={"id": "train-001", "name": "ICE", "device_id": "pico-001"},
        )
        assert created.status_code == 201

        with client.websocket_connect("/ws") as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["data"]["trains"][0]["id"] == "train-001"

            client.post(
                "/api/trains/train-001/command",
                json={"command": "set_speed", "speed": 55},
            )

            # A command produces exactly two pushes: the log event and the
            # new train state.
            updates = [websocket.receive_json() for _ in range(2)]
            train_updates = [m for m in updates if m["type"] == "train.updated"]
            assert train_updates, updates
            assert train_updates[-1]["data"]["speed"] == 55
