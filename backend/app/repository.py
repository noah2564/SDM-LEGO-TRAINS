"""Data access helpers. All SQL lives here."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Event, Train, utcnow


async def list_trains(session: AsyncSession) -> list[Train]:
    result = await session.execute(select(Train).order_by(Train.id))
    return list(result.scalars().all())


async def get_train(session: AsyncSession, train_id: str) -> Train | None:
    return await session.get(Train, train_id)


async def get_train_by_device(session: AsyncSession, device_id: str) -> Train | None:
    result = await session.execute(select(Train).where(Train.device_id == device_id))
    return result.scalars().first()


async def create_train(session: AsyncSession, data: dict[str, Any]) -> Train:
    train = Train(**data)
    session.add(train)
    await session.flush()
    return train


async def update_train(session: AsyncSession, train: Train, data: dict[str, Any]) -> Train:
    for key, value in data.items():
        setattr(train, key, value)
    train.updated_at = utcnow()
    await session.flush()
    return train


async def delete_train(session: AsyncSession, train: Train) -> None:
    await session.delete(train)


async def add_event(
    session: AsyncSession,
    train_id: str,
    event_type: str,
    message: str = "",
    severity: str = "info",
    payload: dict[str, Any] | None = None,
) -> Event:
    event = Event(
        train_id=train_id,
        type=event_type,
        severity=severity,
        message=message,
        payload=payload,
    )
    session.add(event)
    await session.flush()
    return event


async def list_events(session: AsyncSession, train_id: str, limit: int = 50) -> list[Event]:
    result = await session.execute(
        select(Event)
        .where(Event.train_id == train_id)
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def prune_events(session: AsyncSession, train_id: str, keep: int) -> int:
    """Keep only the newest ``keep`` events for a train."""
    result = await session.execute(
        select(Event.id)
        .where(Event.train_id == train_id)
        .order_by(Event.created_at.desc(), Event.id.desc())
        .offset(keep)
    )
    stale_ids = list(result.scalars().all())
    if not stale_ids:
        return 0
    await session.execute(delete(Event).where(Event.id.in_(stale_ids)))
    return len(stale_ids)


async def find_stale_trains(
    session: AsyncSession,
    timeout_seconds: float,
    statuses: tuple[str, ...] = ("online", "connecting"),
) -> list[Train]:
    """Trains in the given statuses that have gone quiet for too long."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    result = await session.execute(select(Train).where(Train.status.in_(statuses)))
    stale: list[Train] = []
    for train in result.scalars().all():
        last_seen = train.last_seen
        if last_seen is None:
            stale.append(train)
            continue
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen < cutoff:
            stale.append(train)
    return stale
