"""Asynchronous SQLAlchemy database infrastructure."""

import asyncio
from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings


class DatabaseReadinessError(RuntimeError):
    """Raised when a database probe returns an unexpected result."""


class Database:
    """Own the engine and session factory behind a replaceable boundary."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.engine: AsyncEngine = create_async_engine(
            self.settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_size=self.settings.database_pool_size,
            max_overflow=self.settings.database_max_overflow,
            pool_recycle=self.settings.database_pool_recycle_seconds,
            connect_args={
                "connect_timeout": self.settings.database_connect_timeout_seconds,
                "init_command": "SET time_zone = '+00:00'",
            },
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a new request-scoped session and close it afterwards."""

        async with self.session_factory() as session:
            yield session

    async def check_connection(self) -> bool:
        """Run a bounded SELECT 1 probe or propagate the underlying error."""

        timeout = self.settings.database_connect_timeout_seconds
        async with asyncio.timeout(timeout):
            async with self.engine.connect() as connection:
                result = await connection.execute(text("SELECT 1"))
                if result.scalar_one() != 1:
                    raise DatabaseReadinessError("database readiness probe failed")
        return True

    async def dispose(self) -> None:
        """Release all engine resources."""

        await self.engine.dispose()


@lru_cache
def get_database() -> Database:
    """Return the process-level database infrastructure object."""

    return Database()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an independent AsyncSession."""

    async for session in get_database().session():
        yield session


async def check_database_connection(database: Database | None = None) -> bool:
    """Check database connectivity through an injectable database object."""

    return await (database or get_database()).check_connection()


async def check_database_readiness(database: Database | None = None) -> bool:
    """Expose the readiness probe for operational validation commands."""

    return await check_database_connection(database)


async def close_database() -> None:
    """Dispose the cached engine during application shutdown."""

    await get_database().dispose()
