"""Shared fixtures.

The suite runs the *real* application wiring (FastAPI, SQLAlchemy, the train
service, the WebSocket hub) against a fake broker, so the only thing not
exercised is the aiomqtt socket itself.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient

from app import repository
from app.api.routes import router
from app.config import Settings
from app.db import Database
from app.train_service import TrainService, serialize_train
from app.ws_hub import Broadcaster


class FakeBroker:
    """An in-process stand-in for Mosquitto.

    It records everything the backend publishes and lets a test subscribe to
    a topic, which is all a simulated Pico needs.
    """

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any], int, bool]] = []
        self.connected = True
        self.subscribers: dict[str, asyncio.Queue[dict[str, Any]]] = {}

    async def publish(
        self, topic: str, payload: dict[str, Any], qos: int = 1, retain: bool = False
    ) -> None:
        if not self.connected:
            raise ConnectionError("broker down")
        self.messages.append((topic, payload, qos, retain))
        queue = self.subscribers.get(topic)
        if queue is not None:
            queue.put_nowait(payload)

    def subscribe(self, topic: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.subscribers[topic] = queue
        return queue

    def commands_for(self, train_id: str) -> list[dict[str, Any]]:
        prefix = f"trains/{train_id}/command"
        return [payload for topic, payload, _, _ in self.messages if topic == prefix]

    def topics(self) -> list[str]:
        return [topic for topic, _, _, _ in self.messages]


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        mqtt_host="localhost",
        offline_timeout_seconds=1.0,
        heartbeat_interval_seconds=0.1,
        monitor_interval_seconds=0.1,
        max_speed=100,
    )


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest_asyncio.fixture
async def database(settings: Settings) -> Database:
    db = Database(settings.database_url)
    await db.create_all()
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
def broadcaster() -> Broadcaster:
    return Broadcaster()


@pytest.fixture
def service(
    database: Database, broker: FakeBroker, broadcaster: Broadcaster, settings: Settings
) -> TrainService:
    return TrainService(database, broker, broadcaster, settings)


@pytest.fixture
def app(service: TrainService, settings: Settings) -> FastAPI:
    """The production routes and WebSocket endpoint, without the MQTT socket."""
    application = FastAPI()
    application.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])
    application.include_router(router, prefix=settings.api_prefix)
    application.state.service = service
    application.state.broadcaster = service.broadcaster

    @application.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await service.broadcaster.register(websocket)
        try:
            async with service.db.session() as session:
                trains = await repository.list_trains(session)
                snapshot = [serialize_train(train) for train in trains]
            await websocket.send_json(
                {"type": "snapshot", "data": {"trains": snapshot, "stats": await service.stats()}}
            )
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await service.broadcaster.unregister(websocket)

    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest_asyncio.fixture
async def train(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/trains",
        json={
            "id": "train-001",
            "name": "ICE",
            "device_id": "pico-001",
            "description": "Test train",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
