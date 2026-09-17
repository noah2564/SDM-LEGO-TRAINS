"""Integration test against a real Mosquitto broker.

Skipped unless MQTT_TEST_HOST is set, so the default suite stays hermetic:

    docker compose up -d mosquitto
    MQTT_TEST_HOST=localhost python -m pytest tests/test_broker_integration.py -v
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from app import protocol
from app.config import Settings
from app.db import Database
from app.mqtt_service import MqttService
from app.train_service import TrainService
from app.ws_hub import Broadcaster

MQTT_TEST_HOST = os.getenv("MQTT_TEST_HOST")

pytestmark = [
    pytest.mark.broker,
    pytest.mark.skipif(not MQTT_TEST_HOST, reason="MQTT_TEST_HOST is not set"),
]


@pytest.mark.asyncio
async def test_round_trip_through_mosquitto(tmp_path) -> None:
    import aiomqtt

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/broker-test.db",
        mqtt_host=MQTT_TEST_HOST or "localhost",
        mqtt_port=int(os.getenv("MQTT_TEST_PORT", "1883")),
        mqtt_username=os.getenv("MQTT_USERNAME") or None,
        mqtt_password=os.getenv("MQTT_PASSWORD") or None,
        mqtt_client_id="lego-backend-test",
        heartbeat_interval_seconds=0.5,
    )
    database = Database(settings.database_url)
    await database.create_all()
    broadcaster = Broadcaster()
    service: TrainService | None = None
    mqtt = MqttService(settings, handler=lambda topic, payload: service.handle_message(topic, payload))
    service = TrainService(database, mqtt, broadcaster, settings)

    await mqtt.start()
    assert await mqtt.wait_connected(timeout=10), "Could not reach the broker"

    train_id, device_id = "train-itest", "pico-itest"
    async with database.session() as session:
        from app import repository

        await repository.create_train(
            session, {"id": train_id, "name": "Integration", "device_id": device_id}
        )

    try:
        async with aiomqtt.Client(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            username=settings.mqtt_username,
            password=settings.mqtt_password,
            identifier="pico-itest",
            will=aiomqtt.Will(
                protocol.status_topic(train_id),
                json.dumps(protocol.offline_will_payload(train_id, device_id)),
                qos=1,
                retain=True,
            ),
        ) as device:
            await device.subscribe(protocol.command_topic(train_id), qos=1)
            await device.publish(
                protocol.status_topic(train_id),
                json.dumps({"status": "online", "device_id": device_id}),
                qos=1,
                retain=True,
            )
            await asyncio.sleep(1.0)

            async with database.session() as session:
                from app import repository

                stored = await repository.get_train(session, train_id)
                assert stored is not None and stored.status == "online"

            # Backend -> broker -> device
            await service.send_command(train_id, {"command": "set_speed", "speed": 33})
            message = await asyncio.wait_for(anext(aiter(device.messages)), timeout=5)
            payload = json.loads(message.payload)
            assert payload["command"] == "set_speed" and payload["speed"] == 33
    finally:
        await mqtt.stop()
        await database.dispose()
