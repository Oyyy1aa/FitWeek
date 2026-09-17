"""Health endpoint unit tests."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

import app.api.health as health_module
from app.config import PersistenceBackend, Settings, get_settings
from app.main import app


class FakeRedisManager:
    def __init__(self, *, enabled: bool, available: bool) -> None:
        self.enabled = enabled
        self.ping = AsyncMock(return_value=available)


async def request_ready(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mysql_available: bool,
    redis_enabled: bool,
    redis_available: bool,
) -> tuple[int, dict[str, object]]:
    mysql_check = AsyncMock(return_value=True)
    if not mysql_available:
        mysql_check = AsyncMock(
            side_effect=RuntimeError(
                "mysql+asyncmy://user:database-secret@private-host/database"
            )
        )
    monkeypatch.setattr(health_module, "check_database_connection", mysql_check)
    monkeypatch.setattr(health_module, "get_database", lambda: object())

    redis_manager = FakeRedisManager(
        enabled=redis_enabled,
        available=redis_available,
    )
    monkeypatch.setattr(health_module, "get_redis_manager", lambda: redis_manager)
    app.dependency_overrides[get_settings] = lambda: Settings(
        persistence_backend=PersistenceBackend.MYSQL,
        redis_enabled=redis_enabled,
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

    assert "database-secret" not in response.text
    assert "private-host" not in response.text
    return response.status_code, response.json()


@pytest.mark.asyncio
async def test_liveness_does_not_access_external_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mysql_check = AsyncMock(side_effect=AssertionError("MySQL must not be used"))
    redis_check = AsyncMock(side_effect=AssertionError("Redis must not be used"))
    monkeypatch.setattr(health_module, "check_database_connection", mysql_check)
    monkeypatch.setattr(health_module, "_redis_status", redis_check)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "fitweek-api"}
    mysql_check.assert_not_awaited()
    redis_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_readiness_does_not_access_external_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health_module,
        "get_database",
        lambda: (_ for _ in ()).throw(AssertionError("MySQL must not be used")),
    )
    monkeypatch.setattr(
        health_module,
        "get_redis_manager",
        lambda: (_ for _ in ()).throw(AssertionError("Redis must not be used")),
    )
    app.dependency_overrides[get_settings] = lambda: Settings(
        persistence_backend=PersistenceBackend.MEMORY,
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
        "mode": "development",
        "checks": {"persistence": "memory", "redis": "disabled"},
        "warnings": [
            "Data is not persistent and will be lost when the process restarts."
        ],
    }


@pytest.mark.parametrize(
    (
        "mysql_available",
        "redis_enabled",
        "redis_available",
        "expected_status_code",
        "expected_body",
    ),
    [
        (
            True,
            True,
            True,
            200,
            {"status": "ready", "checks": {"mysql": "ok", "redis": "ok"}},
        ),
        (
            True,
            True,
            False,
            200,
            {
                "status": "degraded",
                "checks": {"mysql": "ok", "redis": "unavailable"},
            },
        ),
        (
            True,
            False,
            False,
            200,
            {
                "status": "ready",
                "checks": {"mysql": "ok", "redis": "disabled"},
            },
        ),
        (
            False,
            True,
            True,
            503,
            {
                "status": "not_ready",
                "checks": {"mysql": "unavailable", "redis": "ok"},
            },
        ),
        (
            False,
            True,
            False,
            503,
            {
                "status": "not_ready",
                "checks": {"mysql": "unavailable", "redis": "unavailable"},
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_readiness_matrix(
    monkeypatch: pytest.MonkeyPatch,
    mysql_available: bool,
    redis_enabled: bool,
    redis_available: bool,
    expected_status_code: int,
    expected_body: dict[str, object],
) -> None:
    status_code, body = await request_ready(
        monkeypatch,
        mysql_available=mysql_available,
        redis_enabled=redis_enabled,
        redis_available=redis_available,
    )

    assert status_code == expected_status_code
    assert body == expected_body
