"""Redis manager unit tests without a Redis server."""

from unittest.mock import AsyncMock, Mock

import pytest
from redis.exceptions import AuthenticationError, RedisError

from app.config import Settings
from app.infrastructure.redis_client import (
    RedisDisabledError,
    RedisManager,
)


def test_client_uses_bounded_connection_pool_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from_url = Mock()
    monkeypatch.setattr("app.infrastructure.redis_client.Redis.from_url", from_url)

    manager = RedisManager(
        Settings(
            redis_enabled=True,
            redis_max_connections=10,
            redis_connect_timeout_seconds=3,
            redis_socket_timeout_seconds=3,
            redis_healthcheck_interval_seconds=10,
            _env_file=None,
        )
    )

    manager.get_client()

    assert from_url.call_args.kwargs == {
        "decode_responses": True,
        "max_connections": 10,
        "socket_connect_timeout": 3,
        "socket_timeout": 3,
        "health_check_interval": 10,
    }


@pytest.mark.asyncio
async def test_disabled_manager_does_not_create_client() -> None:
    manager = RedisManager(Settings(redis_enabled=False, _env_file=None))

    await manager.start()
    assert await manager.ping() is False
    with pytest.raises(RedisDisabledError):
        manager.get_client()
    await manager.close()


def test_build_key_adds_namespace() -> None:
    manager = RedisManager(Settings(redis_key_prefix="fitweek:test", _env_file=None))

    assert manager.build_key("health", "probe") == "fitweek:test:health:probe"


@pytest.mark.parametrize("parts", [(), ("",), ("ok", " : ")])
def test_build_key_rejects_empty_segments(parts: tuple[str, ...]) -> None:
    manager = RedisManager(Settings(_env_file=None))

    with pytest.raises(ValueError):
        manager.build_key(*parts)


@pytest.mark.asyncio
async def test_close_can_be_called_repeatedly() -> None:
    manager = RedisManager(Settings(_env_file=None))
    client = Mock()
    client.aclose = AsyncMock()
    manager._client = client

    await manager.close()
    await manager.close()

    client.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_ping_normalizes_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RedisManager(Settings(redis_enabled=True, _env_file=None))
    client = Mock()
    client.ping = AsyncMock(side_effect=RedisError("sensitive upstream detail"))
    monkeypatch.setattr(manager, "_ensure_client", lambda: client)

    assert await manager.ping() is False


@pytest.mark.asyncio
async def test_ping_normalizes_authentication_error_without_logging_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = RedisManager(Settings(redis_enabled=True, _env_file=None))
    client = Mock()
    client.ping = AsyncMock(side_effect=AuthenticationError("test-only-secret"))
    monkeypatch.setattr(manager, "_ensure_client", lambda: client)

    assert await manager.ping() is False
    assert "test-only-secret" not in caplog.text


def test_construction_has_no_connection_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from_url = Mock(side_effect=AssertionError("client must remain lazy"))
    monkeypatch.setattr("app.infrastructure.redis_client.Redis.from_url", from_url)

    RedisManager(Settings(redis_enabled=True, _env_file=None))

    from_url.assert_not_called()
