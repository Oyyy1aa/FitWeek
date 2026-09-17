"""MySQL database infrastructure unit tests."""

from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.persistence.database import (
    Database,
    check_database_connection,
    check_database_readiness,
)


@pytest.mark.asyncio
async def test_engine_uses_asyncmy_without_connecting() -> None:
    database = Database(Settings(_env_file=None))
    try:
        assert database.engine.url.drivername == "mysql+asyncmy"
        assert database.engine.pool._recycle == 1800
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_readiness_alias_uses_one_connection_check() -> None:
    database = Database(Settings(_env_file=None))
    database.check_connection = AsyncMock(return_value=True)  # type: ignore[method-assign]
    try:
        assert await check_database_connection(database) is True
        assert await check_database_readiness(database) is True
        assert database.check_connection.await_count == 2
    finally:
        await database.dispose()
