"""REST API. Every route is documented in README.md#api."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import IntegrityError

from .. import protocol, repository
from ..models import Train
from ..schemas import (
    CommandIn,
    CommandResult,
    EventOut,
    FleetStopResult,
    HealthOut,
    SystemStats,
    TrainCreate,
    TrainOut,
    TrainUpdate,
)
from ..train_service import CommandRejected, TrainNotFound, TrainService, serialize_train

logger = logging.getLogger(__name__)
router = APIRouter()

START_TIME = time.monotonic()


def get_service(request: Request) -> TrainService:
    service: TrainService | None = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover - only during startup failure
        raise HTTPException(status_code=503, detail="Service is still starting")
    return service


ServiceDep = Depends(get_service)


# ----------------------------------------------------------------------
# Health and system
# ----------------------------------------------------------------------
@router.get("/health", response_model=HealthOut, tags=["system"])
async def health(service: TrainService = ServiceDep) -> HealthOut:
    database_ok = await service.db.ping()
    return HealthOut(
        status="ok" if database_ok else "degraded",
        database=database_ok,
        mqtt_connected=service.publisher.connected,
        version=protocol.PROTOCOL_VERSION,
        uptime_s=round(time.monotonic() - START_TIME, 1),
    )


@router.get("/stats", response_model=SystemStats, tags=["system"])
async def stats(service: TrainService = ServiceDep) -> SystemStats:
    return SystemStats(**await service.stats())


@router.get("/discovered", tags=["system"])
async def discovered(service: TrainService = ServiceDep) -> list[dict[str, Any]]:
    """Train ids seen on MQTT that are not registered yet."""
    return list(reversed(service.discovered.values()))


# ----------------------------------------------------------------------
# Trains
# ----------------------------------------------------------------------
@router.get("/trains", response_model=list[TrainOut], tags=["trains"])
async def list_trains(service: TrainService = ServiceDep) -> list[TrainOut]:
    async with service.db.session() as session:
        trains = await repository.list_trains(session)
        return [TrainOut.model_validate(train) for train in trains]


@router.get("/trains/{train_id}", response_model=TrainOut, tags=["trains"])
async def get_train(train_id: str, service: TrainService = ServiceDep) -> TrainOut:
    async with service.db.session() as session:
        train = await _require(session, train_id)
        return TrainOut.model_validate(train)


@router.post(
    "/trains", response_model=TrainOut, status_code=status.HTTP_201_CREATED, tags=["trains"]
)
async def create_train(payload: TrainCreate, service: TrainService = ServiceDep) -> TrainOut:
    async with service.db.session() as session:
        if await repository.get_train(session, payload.id) is not None:
            raise HTTPException(status_code=409, detail=f"Train '{payload.id}' already exists")
        if await repository.get_train_by_device(session, payload.device_id) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Device '{payload.device_id}' is already assigned to another train",
            )
        try:
            train = await repository.create_train(session, payload.model_dump())
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Train or device already exists") from exc
        await repository.add_event(session, train.id, "registered", f"Added {train.name}")
        snapshot = serialize_train(train)
        result = TrainOut.model_validate(train)

    service.discovered.pop(payload.id, None)
    await service.broadcaster.broadcast("train.created", snapshot)
    return result


@router.put("/trains/{train_id}", response_model=TrainOut, tags=["trains"])
async def update_train(
    train_id: str, payload: TrainUpdate, service: TrainService = ServiceDep
) -> TrainOut:
    data = payload.model_dump(exclude_unset=True)
    async with service.db.session() as session:
        train = await _require(session, train_id)
        new_device = data.get("device_id")
        if new_device and new_device != train.device_id:
            existing = await repository.get_train_by_device(session, new_device)
            if existing is not None:
                raise HTTPException(
                    status_code=409, detail=f"Device '{new_device}' is already in use"
                )
        train = await repository.update_train(session, train, data)
        await repository.add_event(session, train.id, "updated", "Configuration changed")
        snapshot = serialize_train(train)
        result = TrainOut.model_validate(train)

    await service.broadcaster.broadcast("train.updated", snapshot)
    return result


@router.delete(
    "/trains/{train_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    tags=["trains"],
)
async def delete_train(train_id: str, service: TrainService = ServiceDep) -> None:
    async with service.db.session() as session:
        train = await _require(session, train_id)
        await repository.delete_train(session, train)
    await service.broadcaster.broadcast("train.deleted", {"id": train_id})


@router.get("/trains/{train_id}/events", response_model=list[EventOut], tags=["trains"])
async def train_events(
    train_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    service: TrainService = ServiceDep,
) -> list[EventOut]:
    async with service.db.session() as session:
        await _require(session, train_id)
        events = await repository.list_events(session, train_id, limit)
        return [EventOut.model_validate(event) for event in events]


# ----------------------------------------------------------------------
# Control
# ----------------------------------------------------------------------
@router.post("/trains/{train_id}/command", response_model=CommandResult, tags=["control"])
async def send_command(
    train_id: str, payload: CommandIn, service: TrainService = ServiceDep
) -> CommandResult:
    return await _dispatch(service, train_id, payload.model_dump())


@router.post("/trains/{train_id}/stop", response_model=CommandResult, tags=["control"])
async def stop_train(train_id: str, service: TrainService = ServiceDep) -> CommandResult:
    return await _dispatch(service, train_id, {"command": "stop"})


@router.post("/trains/{train_id}/emergency-stop", response_model=CommandResult, tags=["control"])
async def emergency_stop_train(
    train_id: str,
    reason: str | None = Body(default=None, embed=True),
    service: TrainService = ServiceDep,
) -> CommandResult:
    command: dict[str, Any] = {"command": "emergency_stop"}
    if reason:
        command["reason"] = reason
    return await _dispatch(service, train_id, command)


@router.post(
    "/trains/{train_id}/clear-emergency", response_model=CommandResult, tags=["control"]
)
async def clear_emergency(train_id: str, service: TrainService = ServiceDep) -> CommandResult:
    return await _dispatch(service, train_id, {"command": "clear_emergency"})


@router.post("/emergency-stop", response_model=FleetStopResult, tags=["control"])
async def emergency_stop_all(
    reason: str | None = Body(default=None, embed=True),
    service: TrainService = ServiceDep,
) -> FleetStopResult:
    try:
        stopped = await service.emergency_stop_all(reason or "global emergency stop")
    except CommandRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return FleetStopResult(
        accepted=True,
        trains=stopped,
        detail=f"Emergency stop sent to {len(stopped)} train(s)",
    )


# ----------------------------------------------------------------------
async def _dispatch(
    service: TrainService, train_id: str, command: dict[str, Any]
) -> CommandResult:
    try:
        envelope = await service.send_command(train_id, command, source="web ui")
    except TrainNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Train '{train_id}' not found") from exc
    except protocol.CommandError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CommandRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CommandResult(accepted=True, train_id=train_id, command=envelope)


async def _require(session: Any, train_id: str) -> Train:
    train = await repository.get_train(session, train_id)
    if train is None:
        raise HTTPException(status_code=404, detail=f"Train '{train_id}' not found")
    return train
