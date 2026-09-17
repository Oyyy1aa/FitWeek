"""Redis readiness checks scoped to a unique FitWeek test key."""

import asyncio
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text

import app.api.health as health_module
from app.config import PersistenceBackend, Settings, get_settings
from app.infrastructure.redis_client import RedisManager
from app.main import app
from app.persistence.database import Database


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redis_ping_readiness_ttl_cleanup_and_close(
    test_redis_url: str,
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = f"fitweek:test:{uuid4().hex}"
    manager = RedisManager(
        Settings(
            app_env="test",
            database_url=SecretStr(mysql_test_url),
            persistence_backend=PersistenceBackend.MYSQL,
            redis_enabled=True,
            redis_url=SecretStr(test_redis_url),
            redis_key_prefix=prefix,
            redis_max_connections=10,
            redis_connect_timeout_seconds=3,
            redis_socket_timeout_seconds=3,
            redis_healthcheck_interval_seconds=10,
            _env_file=None,
        )
    )
    key = manager.build_key("integration", "probe")
    client = manager.get_client()
    unavailable_manager: RedisManager | None = None
    monkeypatch.setattr(health_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(health_module, "get_redis_manager", lambda: manager)
    app.dependency_overrides[get_settings] = lambda: manager.settings
    try:
        assert key.startswith(f"{prefix}:")
        assert await manager.ping() is True
        assert await client.set(key, "ok", ex=30) is True
        assert await client.get(key) == "ok"
        assert await client.ttl(key) > 0

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as http_client:
            response = await http_client.get("/health/ready")

        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "checks": {"mysql": "ok", "redis": "ok"},
        }
        password = urlsplit(test_redis_url).password
        if password is not None:
            assert password not in response.text
        assert test_redis_url not in response.text

        unavailable_manager = RedisManager(
            Settings(
                app_env="test",
                database_url=SecretStr(mysql_test_url),
                persistence_backend=PersistenceBackend.MYSQL,
                redis_enabled=True,
                redis_url=SecretStr("redis://:test-only-secret@127.0.0.1:1/15"),
                redis_key_prefix=prefix,
                redis_max_connections=10,
                redis_connect_timeout_seconds=0.1,
                redis_socket_timeout_seconds=0.1,
                redis_healthcheck_interval_seconds=10,
                _env_file=None,
            )
        )
        monkeypatch.setattr(
            health_module, "get_redis_manager", lambda: unavailable_manager
        )
        async with asyncio.timeout(1):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as http_client:
                degraded_response = await http_client.get("/health/ready")
        assert degraded_response.status_code == 200
        assert degraded_response.json() == {
            "status": "degraded",
            "checks": {"mysql": "ok", "redis": "unavailable"},
        }
        assert "test-only-secret" not in degraded_response.text
        async with mysql_test_database.session_factory() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        monkeypatch.setattr(health_module, "get_redis_manager", lambda: manager)
        assert await manager.ping() is True
    finally:
        await client.delete(key)
        await manager.close()
        if unavailable_manager is not None:
            await unavailable_manager.close()
        app.dependency_overrides.pop(get_settings, None)

    assert manager._client is None
