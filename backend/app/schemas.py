"""Request and response schemas for the REST API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,63}$")


def _validate_identifier(value: str, label: str) -> str:
    value = value.strip()
    if not ID_PATTERN.match(value):
        raise ValueError(
            f"{label} must be 2-64 chars of letters, digits, '-', '_' or '.' "
            "(it is used verbatim in MQTT topics)"
        )
    if "+" in value or "#" in value or "/" in value:
        raise ValueError(f"{label} must not contain MQTT wildcards or '/'")
    return value


class TrainCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Logical train id, e.g. 'train-001'")
    name: str = Field(min_length=1, max_length=120)
    device_id: str = Field(description="Pico W device id, e.g. 'pico-001'")
    description: str | None = Field(default=None, max_length=1000)
    max_speed: int = Field(default=100, ge=1, le=100)
    configuration: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _validate_identifier(value, "Train id")

    @field_validator("device_id")
    @classmethod
    def _check_device_id(cls, value: str) -> str:
        return _validate_identifier(value, "Device id")


class TrainUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    device_id: str | None = None
    description: str | None = Field(default=None, max_length=1000)
    max_speed: int | None = Field(default=None, ge=1, le=100)
    configuration: dict[str, Any] | None = None

    @field_validator("device_id")
    @classmethod
    def _check_device_id(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value, "Device id")


class TrainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    device_id: str
    description: str | None
    status: str
    speed: int
    direction: str
    emergency_stop: bool
    battery: float | None
    max_speed: int
    configuration: dict[str, Any]
    last_telemetry: dict[str, Any] | None
    last_seen: datetime | None
    last_message_at: datetime | None
    last_command_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    train_id: str
    type: str
    severity: str
    message: str
    payload: dict[str, Any] | None
    created_at: datetime


class CommandIn(BaseModel):
    """A raw command as posted by the UI. Validated in app.protocol."""

    model_config = ConfigDict(extra="allow")

    command: str


class CommandResult(BaseModel):
    accepted: bool
    train_id: str
    command: dict[str, Any]
    detail: str | None = None


class FleetStopResult(BaseModel):
    accepted: bool
    trains: list[str]
    detail: str


class SystemStats(BaseModel):
    total: int
    online: int
    offline: int
    connecting: int
    unknown: int
    moving: int
    emergency_stopped: int
    mqtt_connected: bool


class HealthOut(BaseModel):
    status: str
    database: bool
    mqtt_connected: bool
    version: int
    uptime_s: float
