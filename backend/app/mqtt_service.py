"""MQTT connectivity.

The rest of the application depends only on the :class:`Publisher` protocol,
which keeps the broker out of the unit tests and makes the transport
replaceable.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import aiomqtt

from . import protocol
from .config import Settings

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str, bytes], Awaitable[None]]


class Publisher(Protocol):
    """Anything capable of putting a message on the broker."""

    @property
    def connected(self) -> bool: ...

    async def publish(
        self, topic: str, payload: dict[str, Any], qos: int = 1, retain: bool = False
    ) -> None: ...


class MqttService:
    """Maintains a single broker connection, re-establishing it on failure."""

    def __init__(self, settings: Settings, handler: MessageHandler) -> None:
        self._settings = settings
        self._handler = handler
        self._client: aiomqtt.Client | None = None
        self._connected = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.on_connect: Callable[[], Awaitable[None]] | None = None
        self.connection_attempts = 0
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="mqtt-loop")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - shutdown noise
                logger.warning("MQTT loop ended with %s", exc)
            self._task = None
        self._connected.clear()

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def publish(
        self, topic: str, payload: dict[str, Any], qos: int = 1, retain: bool = False
    ) -> None:
        client = self._client
        if client is None or not self._connected.is_set():
            raise ConnectionError("MQTT broker is not connected")
        await client.publish(topic, protocol.encode(payload), qos=qos, retain=retain)
        logger.debug("Published to %s: %s", topic, payload)

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        settings = self._settings
        while not self._stopping:
            self.connection_attempts += 1
            try:
                async with aiomqtt.Client(
                    hostname=settings.mqtt_host,
                    port=settings.mqtt_port,
                    username=settings.mqtt_username or None,
                    password=settings.mqtt_password or None,
                    identifier=settings.mqtt_client_id,
                    keepalive=settings.mqtt_keepalive,
                ) as client:
                    self._client = client
                    self._connected.set()
                    self.last_error = None
                    logger.info(
                        "Connected to MQTT broker %s:%s", settings.mqtt_host, settings.mqtt_port
                    )
                    for topic in protocol.SUBSCRIPTIONS:
                        await client.subscribe(topic, qos=1)
                        logger.info("Subscribed to %s", topic)
                    if self.on_connect is not None:
                        await self.on_connect()
                    async for message in client.messages:
                        try:
                            await self._handler(str(message.topic), bytes(message.payload or b""))
                        except Exception:
                            # One bad message must never kill the ingest loop.
                            logger.exception(
                                "Error handling MQTT message on %s", message.topic
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.warning(
                    "MQTT connection lost (%s). Retrying in %.1fs",
                    exc,
                    settings.mqtt_reconnect_delay,
                )
            finally:
                self._connected.clear()
                self._client = None
            if self._stopping:
                break
            await asyncio.sleep(settings.mqtt_reconnect_delay)
