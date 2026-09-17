"""The system logic: everything that happens between MQTT and the API.

Nothing in this module talks to a real broker or a real HTTP client, which is
what makes the whole pipeline testable end to end in process.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from . import protocol, repository
from .config import Settings
from .db import Database
from .models import Train, utcnow
from .mqtt_service import Publisher
from .schemas import EventOut, TrainOut
from .ws_hub import Broadcaster

logger = logging.getLogger(__name__)


class TrainNotFound(LookupError):
    pass


class CommandRejected(Exception):
    """A command was well-formed but must not be sent in the current state."""


def serialize_train(train: Train) -> dict[str, Any]:
    return TrainOut.model_validate(train).model_dump(mode="json")


class TrainService:
    def __init__(
        self,
        database: Database,
        publisher: Publisher,
        broadcaster: Broadcaster,
        settings: Settings,
    ) -> None:
        self.db = database
        self.publisher = publisher
        self.broadcaster = broadcaster
        self.settings = settings
        #: train_ids seen on MQTT that are not registered yet -> last seen ISO ts
        self.discovered: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._tasks: list[asyncio.Task[None]] = []

    # ==================================================================
    # Inbound: MQTT -> database -> WebSocket
    # ==================================================================
    async def handle_message(self, topic: str, payload: bytes) -> None:
        parsed = protocol.parse_topic(topic)
        if parsed is None:
            logger.debug("Ignoring message on unhandled topic %s", topic)
            return
        train_id, kind = parsed
        if kind == "command":
            return  # our own outbound traffic

        try:
            data = protocol.decode(payload)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            logger.warning("Malformed payload on %s: %s", topic, exc)
            return

        async with self.db.session() as session:
            train = await repository.get_train(session, train_id)
            if train is None:
                self._remember_discovery(train_id, kind, data)
                logger.info(
                    "Message from unregistered train '%s' on %s - add it to control it",
                    train_id,
                    kind,
                )
                await self.broadcaster.broadcast(
                    "discovery", {"train_id": train_id, "kind": kind}
                )
                return

            if kind == "status":
                await self._apply_status(session, train, data)
            elif kind == "telemetry":
                await self._apply_telemetry(session, train, data)
            elif kind == "event":
                await self._apply_event(session, train, data)

            snapshot = serialize_train(train)

        await self.broadcaster.broadcast("train.updated", snapshot)

    async def _apply_status(
        self, session: AsyncSession, train: Train, payload: dict[str, Any]
    ) -> None:
        clean = protocol.sanitize_status(payload)
        previous = train.status
        train.status = clean["status"]
        train.last_message_at = utcnow()
        if train.status in {"online", "connecting"}:
            train.last_seen = utcnow()
        if train.status == "offline":
            # A train we cannot reach is not moving as far as we know.
            train.speed = 0
        if clean.get("device_id") and clean["device_id"] != train.device_id:
            logger.warning(
                "Train %s reports device_id '%s' but is registered as '%s'",
                train.id,
                clean["device_id"],
                train.device_id,
            )
            train.last_error = (
                f"Device id mismatch: reported {clean['device_id']}, expected {train.device_id}"
            )
        train.updated_at = utcnow()

        if previous != train.status:
            reason = clean.get("reason")
            message = f"{previous} -> {train.status}" + (f" ({reason})" if reason else "")
            severity = "warning" if train.status in {"offline", "unknown"} else "info"
            await self._record_event(session, train, "status_change", message, severity, clean)
            logger.info("Train %s status %s", train.id, message)

    async def _apply_telemetry(
        self, session: AsyncSession, train: Train, payload: dict[str, Any]
    ) -> None:
        clean = protocol.sanitize_telemetry(payload, max_speed=train.max_speed)
        train.last_telemetry = clean
        train.last_message_at = utcnow()
        train.last_seen = utcnow()
        if "speed" in clean:
            train.speed = clean["speed"]
        if "direction" in clean:
            train.direction = clean["direction"]
        if "battery" in clean:
            train.battery = clean["battery"]
        if "emergency_stop" in clean:
            train.emergency_stop = bool(clean["emergency_stop"])
        if train.status != "online":
            previous = train.status
            train.status = "online"
            await self._record_event(
                session, train, "status_change", f"{previous} -> online (telemetry)", "info"
            )
        train.updated_at = utcnow()

    async def _apply_event(
        self, session: AsyncSession, train: Train, payload: dict[str, Any]
    ) -> None:
        clean = protocol.sanitize_event(payload)
        train.last_message_at = utcnow()
        train.last_seen = utcnow()
        if clean["severity"] == "error":
            train.last_error = clean["message"] or clean["type"]
        await self._record_event(
            session, train, clean["type"], clean["message"], clean["severity"], payload
        )
        train.updated_at = utcnow()

    async def _record_event(
        self,
        session: AsyncSession,
        train: Train,
        event_type: str,
        message: str,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = await repository.add_event(
            session, train.id, event_type, message, severity, payload
        )
        await repository.prune_events(session, train.id, self.settings.max_events_per_train)
        await self.broadcaster.broadcast(
            "event", EventOut.model_validate(event).model_dump(mode="json")
        )

    def _remember_discovery(self, train_id: str, kind: str, payload: dict[str, Any]) -> None:
        entry = self.discovered.get(train_id, {"train_id": train_id})
        entry["last_seen"] = protocol.utcnow_iso()
        entry["last_topic"] = kind
        device_id = payload.get("device_id")
        if isinstance(device_id, str):
            entry["device_id"] = device_id[:64]
        self.discovered[train_id] = entry
        self.discovered.move_to_end(train_id)
        while len(self.discovered) > 50:
            self.discovered.popitem(last=False)

    # ==================================================================
    # Outbound: API -> validation -> MQTT
    # ==================================================================
    async def send_command(
        self, train_id: str, payload: dict[str, Any], *, source: str = "api"
    ) -> dict[str, Any]:
        """Validate and publish a command. Returns the published envelope."""
        async with self.db.session() as session:
            train = await repository.get_train(session, train_id)
            if train is None:
                raise TrainNotFound(train_id)
            max_speed = train.max_speed

            # protocol.validate_command raises CommandError for bad input.
            command = protocol.validate_command(payload, max_speed=max_speed)
            name = command["command"]

            if train.emergency_stop and name not in protocol.SAFETY_COMMANDS:
                raise CommandRejected(
                    "Train is emergency stopped. Clear the emergency stop before driving."
                )

            envelope = protocol.build_command_envelope(train.id, command)
            await self._publish_command(train.id, envelope)

            self._apply_optimistic_state(train, command)
            train.last_command_at = utcnow()
            train.updated_at = utcnow()
            await self._record_event(
                session,
                train,
                f"command.{name}",
                f"{name} sent via {source}",
                "warning" if name == "emergency_stop" else "info",
                envelope,
            )
            snapshot = serialize_train(train)

        await self.broadcaster.broadcast("train.updated", snapshot)
        return envelope

    def _apply_optimistic_state(self, train: Train, command: dict[str, Any]) -> None:
        """Reflect the command immediately; telemetry is the source of truth."""
        name = command["command"]
        if name == "set_speed":
            train.speed = int(command["speed"])
            if command.get("direction"):
                train.direction = command["direction"]
        elif name == "set_direction":
            train.direction = command["direction"]
        elif name == "stop":
            train.speed = 0
        elif name == "emergency_stop":
            train.speed = 0
            train.emergency_stop = True
        elif name == "clear_emergency":
            train.emergency_stop = False
            train.speed = 0

    async def _publish_command(self, train_id: str, envelope: dict[str, Any]) -> None:
        try:
            await self.publisher.publish(
                protocol.command_topic(train_id), envelope, qos=1, retain=False
            )
        except ConnectionError as exc:
            raise CommandRejected(f"Cannot reach the MQTT broker: {exc}") from exc

    async def stop_train(self, train_id: str) -> dict[str, Any]:
        return await self.send_command(train_id, {"command": "stop"}, source="stop button")

    async def emergency_stop_train(self, train_id: str, reason: str | None = None) -> dict[str, Any]:
        command: dict[str, Any] = {"command": "emergency_stop"}
        if reason:
            command["reason"] = reason
        return await self.send_command(train_id, command, source="emergency stop")

    async def clear_emergency(self, train_id: str) -> dict[str, Any]:
        return await self.send_command(train_id, {"command": "clear_emergency"}, source="api")

    async def emergency_stop_all(self, reason: str = "global emergency stop") -> list[str]:
        """Broadcast, then target every known train individually.

        The broadcast reaches devices in one hop; the per-train publishes make
        the stop work even for firmware that only subscribes to its own topic,
        and they are what update our persisted state.
        """
        envelope = protocol.build_command_envelope(
            "*", {"command": "emergency_stop", "reason": reason}
        )
        broadcast_failed: str | None = None
        try:
            await self.publisher.publish(
                protocol.SYSTEM_COMMAND_TOPIC, envelope, qos=1, retain=False
            )
        except ConnectionError as exc:
            broadcast_failed = str(exc)
            logger.error("Global emergency stop broadcast failed: %s", exc)

        stopped: list[str] = []
        async with self.db.session() as session:
            trains = await repository.list_trains(session)
            for train in trains:
                per_train = protocol.build_command_envelope(
                    train.id, {"command": "emergency_stop", "reason": reason}
                )
                try:
                    await self._publish_command(train.id, per_train)
                except CommandRejected as exc:
                    logger.error("Emergency stop for %s failed: %s", train.id, exc)
                    continue
                train.speed = 0
                train.emergency_stop = True
                train.last_command_at = utcnow()
                train.updated_at = utcnow()
                await self._record_event(
                    session, train, "command.emergency_stop", reason, "warning", per_train
                )
                stopped.append(train.id)
            snapshots = [serialize_train(train) for train in trains]

        for snapshot in snapshots:
            await self.broadcaster.broadcast("train.updated", snapshot)
        await self.broadcaster.broadcast(
            "fleet.emergency_stop", {"trains": stopped, "reason": reason}
        )
        if broadcast_failed and not stopped:
            raise CommandRejected(f"Cannot reach the MQTT broker: {broadcast_failed}")
        return stopped

    # ==================================================================
    # Background tasks
    # ==================================================================
    async def start_background_tasks(self) -> None:
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._monitor_loop(), name="staleness-monitor"),
        ]

    async def stop_background_tasks(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []

    async def _heartbeat_loop(self) -> None:
        """Prove to the fleet that the backend is alive.

        Firmware uses the absence of this beacon as its cue to stop the motor,
        so this loop is a safety component, not telemetry.
        """
        while True:
            try:
                if self.publisher.connected:
                    await self.publisher.publish(
                        protocol.SYSTEM_HEARTBEAT_TOPIC,
                        protocol.heartbeat_payload(),
                        qos=0,
                        retain=False,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Heartbeat publish failed: %s", exc)
            await asyncio.sleep(self.settings.heartbeat_interval_seconds)

    async def _monitor_loop(self) -> None:
        """Downgrade trains that stop talking, even without an LWT."""
        timeout = self.settings.offline_timeout_seconds
        while True:
            try:
                changed: list[dict[str, Any]] = []
                async with self.db.session() as session:
                    for train in await repository.find_stale_trains(session, timeout):
                        await self._downgrade(session, train, "unknown", timeout)
                        changed.append(serialize_train(train))
                    for train in await repository.find_stale_trains(
                        session, timeout * 3, statuses=("unknown",)
                    ):
                        await self._downgrade(session, train, "offline", timeout * 3)
                        changed.append(serialize_train(train))
                for snapshot in changed:
                    await self.broadcaster.broadcast("train.updated", snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Staleness monitor iteration failed")
            await asyncio.sleep(self.settings.monitor_interval_seconds)

    async def _downgrade(
        self, session: AsyncSession, train: Train, status: str, timeout: float
    ) -> None:
        previous = train.status
        train.status = status
        train.speed = 0
        train.updated_at = utcnow()
        await self._record_event(
            session,
            train,
            "status_change",
            f"{previous} -> {status} (no message for {timeout:.0f}s)",
            "warning",
        )
        logger.warning("Train %s went %s after %.0fs of silence", train.id, status, timeout)

    # ==================================================================
    # Queries
    # ==================================================================
    async def stats(self) -> dict[str, Any]:
        async with self.db.session() as session:
            trains = await repository.list_trains(session)
        counts = {"online": 0, "offline": 0, "connecting": 0, "unknown": 0}
        moving = 0
        estopped = 0
        for train in trains:
            counts[train.status] = counts.get(train.status, 0) + 1
            if train.status == "online" and train.speed > 0:
                moving += 1
            if train.emergency_stop:
                estopped += 1
        return {
            "total": len(trains),
            "online": counts["online"],
            "offline": counts["offline"],
            "connecting": counts["connecting"],
            "unknown": counts["unknown"],
            "moving": moving,
            "emergency_stopped": estopped,
            "mqtt_connected": self.publisher.connected,
        }

    async def publish_initial_state(self) -> None:
        """Called after an MQTT (re)connect.

        Retained status messages are replayed by the broker automatically, so
        we only need to nudge the devices for fresh telemetry.
        """
        async with self.db.session() as session:
            trains = await repository.list_trains(session)
        for train in trains:
            try:
                envelope = protocol.build_command_envelope(train.id, {"command": "ping"})
                await self.publisher.publish(protocol.command_topic(train.id), envelope, qos=0)
            except Exception as exc:
                logger.debug("Ping for %s failed: %s", train.id, exc)


def as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
