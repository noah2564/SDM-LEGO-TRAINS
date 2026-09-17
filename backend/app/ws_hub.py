"""Fan-out of backend events to connected browsers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class WebSocketLike(Protocol):
    async def send_json(self, data: Any) -> None: ...


class Broadcaster:
    """Tracks connected WebSocket clients and pushes JSON messages to them.

    A slow or dead client is dropped rather than allowed to block the MQTT
    ingest path.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocketLike] = set()
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def register(self, websocket: WebSocketLike) -> None:
        async with self._lock:
            self._clients.add(websocket)
        logger.debug("WebSocket connected (%d total)", len(self._clients))

    async def unregister(self, websocket: WebSocketLike) -> None:
        async with self._lock:
            self._clients.discard(websocket)
        logger.debug("WebSocket disconnected (%d total)", len(self._clients))

    async def broadcast(self, message_type: str, data: Any) -> None:
        if not self._clients:
            return
        message = {"type": message_type, "data": data}
        async with self._lock:
            targets = list(self._clients)
        dead: list[WebSocketLike] = []
        for client in targets:
            try:
                await client.send_json(message)
            except Exception as exc:
                logger.debug("Dropping WebSocket client: %s", exc)
                dead.append(client)
        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.discard(client)
