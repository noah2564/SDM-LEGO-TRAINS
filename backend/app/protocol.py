"""The wire protocol shared by the backend, the Pico W firmware and the simulator.

Topic scheme
------------
    trains/{train_id}/status      device -> backend, retained, also the LWT topic
    trains/{train_id}/telemetry   device -> backend
    trains/{train_id}/event       device -> backend
    trains/{train_id}/command     backend -> device
    system/command                backend -> all devices (broadcast, e.g. global stop)
    system/heartbeat              backend -> all devices (liveness beacon)

Rationale for the two `system/*` topics:

* A global emergency stop published once on ``system/command`` reaches every
  device in a single broker round trip instead of N publishes. The backend
  *also* publishes to each train's own command topic so that the fleet stops
  even if a device only subscribed to its own topic.
* ``system/heartbeat`` lets each device detect that the *backend* has died,
  not merely that the broker link is up. Devices stop when the beacon stops.

Message envelopes are JSON objects. Every consumer must ignore unknown keys,
which is what makes the format forwards-compatible: new telemetry fields or
new command parameters can be added without breaking older firmware.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION: Final[int] = 1

TOPIC_ROOT: Final[str] = "trains"
SYSTEM_COMMAND_TOPIC: Final[str] = "system/command"
SYSTEM_HEARTBEAT_TOPIC: Final[str] = "system/heartbeat"

SUBSCRIPTIONS: Final[tuple[str, ...]] = (
    "trains/+/status",
    "trains/+/telemetry",
    "trains/+/event",
)

Direction = Literal["forward", "backward"]
TrainStatus = Literal["online", "offline", "connecting", "unknown"]

VALID_DIRECTIONS: Final[frozenset[str]] = frozenset({"forward", "backward"})
VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {"online", "offline", "connecting", "unknown"}
)


# --------------------------------------------------------------------------
# Topic helpers
# --------------------------------------------------------------------------
def status_topic(train_id: str) -> str:
    return f"{TOPIC_ROOT}/{train_id}/status"


def telemetry_topic(train_id: str) -> str:
    return f"{TOPIC_ROOT}/{train_id}/telemetry"


def event_topic(train_id: str) -> str:
    return f"{TOPIC_ROOT}/{train_id}/event"


def command_topic(train_id: str) -> str:
    return f"{TOPIC_ROOT}/{train_id}/command"


def parse_topic(topic: str) -> tuple[str, str] | None:
    """Return ``(train_id, kind)`` for a device topic, or None if unrecognised."""
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != TOPIC_ROOT:
        return None
    train_id, kind = parts[1], parts[2]
    if not train_id or kind not in {"status", "telemetry", "event", "command"}:
        return None
    return train_id, kind


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# Commands (backend -> device)
# --------------------------------------------------------------------------
class CommandError(ValueError):
    """Raised when a command fails validation and must not be published."""


class BaseCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str


class SetSpeedCommand(BaseCommand):
    command: Literal["set_speed"]
    speed: int = Field(ge=0, le=100)
    direction: Direction | None = None


class SetDirectionCommand(BaseCommand):
    command: Literal["set_direction"]
    direction: Direction


class StopCommand(BaseCommand):
    command: Literal["stop"]


class EmergencyStopCommand(BaseCommand):
    command: Literal["emergency_stop"]
    reason: str | None = Field(default=None, max_length=200)


class ClearEmergencyCommand(BaseCommand):
    command: Literal["clear_emergency"]


class SetConfigCommand(BaseCommand):
    """Push runtime tuning to a device without reflashing it."""

    command: Literal["set_config"]
    safety_timeout_s: float | None = Field(default=None, ge=1.0, le=120.0)
    telemetry_interval_s: float | None = Field(default=None, ge=0.2, le=60.0)

    @model_validator(mode="after")
    def _at_least_one(self) -> "SetConfigCommand":
        if self.safety_timeout_s is None and self.telemetry_interval_s is None:
            raise ValueError("set_config requires safety_timeout_s or telemetry_interval_s")
        return self


class PingCommand(BaseCommand):
    command: Literal["ping"]


COMMAND_MODELS: Final[dict[str, type[BaseCommand]]] = {
    "set_speed": SetSpeedCommand,
    "set_direction": SetDirectionCommand,
    "stop": StopCommand,
    "emergency_stop": EmergencyStopCommand,
    "clear_emergency": ClearEmergencyCommand,
    "set_config": SetConfigCommand,
    "ping": PingCommand,
}

#: Commands that must be honoured even while the emergency-stop latch is set.
SAFETY_COMMANDS: Final[frozenset[str]] = frozenset(
    {"stop", "emergency_stop", "clear_emergency", "ping", "set_config"}
)


def validate_command(payload: dict[str, Any], max_speed: int = 100) -> dict[str, Any]:
    """Validate an inbound command and return its normalised form.

    Raises :class:`CommandError` for anything that must not reach a train.
    """
    if not isinstance(payload, dict):
        raise CommandError("Command payload must be a JSON object")

    name = payload.get("command")
    if not isinstance(name, str) or not name:
        raise CommandError("Missing 'command' field")

    model = COMMAND_MODELS.get(name)
    if model is None:
        known = ", ".join(sorted(COMMAND_MODELS))
        raise CommandError(f"Unknown command '{name}'. Known commands: {known}")

    try:
        parsed = model.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError
        raise CommandError(f"Invalid '{name}' command: {_first_error(exc)}") from exc

    data = parsed.model_dump(exclude_none=True)

    if isinstance(parsed, SetSpeedCommand) and parsed.speed > max_speed:
        raise CommandError(f"Speed {parsed.speed} exceeds configured limit of {max_speed}")

    return data


def _first_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            location = ".".join(str(part) for part in first.get("loc", ())) or "payload"
            return f"{location}: {first.get('msg', 'invalid')}"
        except Exception:  # pragma: no cover - defensive
            pass
    return str(exc)


def build_command_envelope(train_id: str, command: dict[str, Any]) -> dict[str, Any]:
    """Wrap a validated command in the envelope that devices expect."""
    return {
        "v": PROTOCOL_VERSION,
        "command_id": uuid.uuid4().hex[:12],
        "train_id": train_id,
        "ts": utcnow_iso(),
        **command,
    }


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def decode(payload: bytes | str) -> dict[str, Any]:
    """Decode a device payload; raises ValueError if it is not a JSON object."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Payload must be a JSON object")
    return data


# --------------------------------------------------------------------------
# Device -> backend payload sanitising
#
# Devices are not trusted. Everything coming off the wire is clamped and
# type-checked before it is allowed anywhere near the database or the UI.
# --------------------------------------------------------------------------
def sanitize_status(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status")
    if not isinstance(status, str) or status not in VALID_STATUSES:
        status = "unknown"
    result: dict[str, Any] = {"status": status}
    device_id = payload.get("device_id")
    if isinstance(device_id, str) and device_id.strip():
        result["device_id"] = device_id.strip()[:64]
    for key in ("firmware", "ip", "reason"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()[:64]
    return result


def sanitize_telemetry(payload: dict[str, Any], max_speed: int = 100) -> dict[str, Any]:
    """Clamp and coerce telemetry. Unknown keys are preserved under 'extra'."""
    known = {
        "v",
        "train_id",
        "timestamp",
        "ts",
        "speed",
        "direction",
        "battery",
        "connected",
        "emergency_stop",
        "uptime_s",
        "rssi",
        "hub_connected",
    }
    result: dict[str, Any] = {}

    speed = _as_number(payload.get("speed"))
    if speed is not None:
        result["speed"] = int(max(0, min(max_speed, round(speed))))

    direction = payload.get("direction")
    if isinstance(direction, str) and direction in VALID_DIRECTIONS:
        result["direction"] = direction

    battery = _as_number(payload.get("battery"))
    if battery is not None:
        result["battery"] = round(float(max(0.0, min(30.0, battery))), 3)

    for flag in ("connected", "emergency_stop", "hub_connected"):
        value = payload.get(flag)
        if isinstance(value, bool):
            result[flag] = value

    uptime = _as_number(payload.get("uptime_s"))
    if uptime is not None and uptime >= 0:
        result["uptime_s"] = round(float(uptime), 1)

    rssi = _as_number(payload.get("rssi"))
    if rssi is not None:
        result["rssi"] = int(max(-200, min(0, round(rssi))))

    extra = {
        key: value
        for key, value in payload.items()
        if key not in known and isinstance(value, (str, int, float, bool))
    }
    if extra:
        # Bounded so a chatty device cannot bloat the database.
        result["extra"] = dict(list(extra.items())[:16])

    result["received_at"] = utcnow_iso()
    return result


def sanitize_event(payload: dict[str, Any]) -> dict[str, Any]:
    event_type = payload.get("type") or payload.get("event") or "device_event"
    message = payload.get("message") or payload.get("msg") or ""
    severity = payload.get("severity")
    if severity not in {"info", "warning", "error"}:
        severity = "info"
    return {
        "type": str(event_type)[:64],
        "message": str(message)[:500],
        "severity": severity,
    }


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def offline_will_payload(train_id: str, device_id: str | None = None) -> dict[str, Any]:
    """The payload a device should register as its Last Will and Testament."""
    payload: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "train_id": train_id,
        "status": "offline",
        "reason": "lwt",
    }
    if device_id:
        payload["device_id"] = device_id
    return payload


def heartbeat_payload() -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "ts": utcnow_iso(), "epoch": int(time.time())}
