"""MySQL compatibility checks against an isolated test database."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

import app.api.health as health_module
from app.config import PersistenceBackend, Settings, get_settings
from app.infrastructure.redis_client import RedisManager
from app.main import app
from app.persistence.database import Database


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_select_one_and_session_cleanup(
    mysql_test_database: Database,
) -> None:
    assert await mysql_test_database.check_connection() is True
    async with mysql_test_database.session_factory() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ready_endpoint_with_mysql(
    mysql_test_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_redis = RedisManager(Settings(redis_enabled=False, _env_file=None))
    monkeypatch.setattr(health_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(health_module, "get_redis_manager", lambda: disabled_redis)
    app.dependency_overrides[get_settings] = lambda: Settings(
        persistence_backend=PersistenceBackend.MYSQL,
        redis_enabled=False,
        _env_file=None,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/health/ready")
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"mysql": "ok", "redis": "disabled"},
    }
