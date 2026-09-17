"""Database engine and session management."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

logger = logging.getLogger(__name__)


def create_engine(database_url: str) -> AsyncEngine:
    """Create the async engine, preparing the on-disk location if needed."""
    if database_url.startswith("sqlite"):
        path = database_url.split("///", 1)[-1]
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        return create_async_engine(database_url, future=True, echo=False)
    # PostgreSQL and friends benefit from connection pre-ping.
    return create_async_engine(database_url, future=True, echo=False, pool_pre_ping=True)


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, database_url: str) -> None:
        self.url = database_url
        self.engine = create_engine(database_url)
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def create_all(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.info("Database ready at %s", _redact(self.url))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def ping(self) -> bool:
        from sqlalchemy import text

        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.error("Database ping failed: %s", exc)
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()


def _redact(url: str) -> str:
    if "@" in url and "//" in url:
        scheme, rest = url.split("//", 1)
        return f"{scheme}//***@{rest.split('@', 1)[1]}"
    return url
