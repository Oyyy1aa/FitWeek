"""Configuration unit tests."""

import pytest
from pydantic import SecretStr
from pydantic_core import ValidationError

from app.config import PersistenceBackend, Settings


def test_default_configuration_loads_without_environment() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "FitWeek"
    assert settings.app_port == 8000
    assert settings.database_connect_timeout_seconds == 3.0
    assert settings.database_url.get_secret_value().startswith("mysql+asyncmy://")
    assert settings.redis_url.get_secret_value().startswith("redis://")
    assert settings.persistence_backend is PersistenceBackend.MYSQL
    assert settings.redis_enabled is False


def test_environment_overrides_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_PORT", "8123")
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "7")
    monkeypatch.setenv("REDIS_KEY_PREFIX", "fitweek:test")
    monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "9")
    monkeypatch.setenv("REDIS_HEALTH_CHECK_INTERVAL_SECONDS", "11")
    monkeypatch.setenv("PERSISTENCE_BACKEND", "mysql")

    settings = Settings(_env_file=None)

    assert settings.app_env == "test"
    assert settings.app_port == 8123
    assert settings.database_connect_timeout_seconds == 1.5
    assert settings.database_pool_size == 7
    assert settings.redis_key_prefix == "fitweek:test"
    assert settings.redis_max_connections == 9
    assert settings.redis_healthcheck_interval_seconds == 11
    assert settings.persistence_backend is PersistenceBackend.MYSQL


def test_connection_urls_are_masked_in_string_representations() -> None:
    database_password = "database-not-for-logs"
    redis_password = "redis-not-for-logs"
    settings = Settings(
        database_url=SecretStr(
            f"mysql+asyncmy://fitweek:{database_password}@127.0.0.1/fitweek_test"
        ),
        redis_url=SecretStr(f"redis://default:{redis_password}@127.0.0.1:6379/15"),
        _env_file=None,
    )

    assert database_password not in str(settings)
    assert database_password not in repr(settings)
    assert redis_password not in str(settings)
    assert redis_password not in repr(settings)
    assert "**********" in str(settings)


def test_redis_can_be_disabled() -> None:
    settings = Settings(redis_enabled=False, _env_file=None)

    assert settings.redis_enabled is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_connect_timeout_seconds", 0),
        ("database_pool_size", 0),
        ("database_pool_recycle_seconds", 0),
        ("redis_connect_timeout_seconds", 0),
        ("redis_socket_timeout_seconds", 0),
        ("redis_max_connections", 0),
        ("redis_healthcheck_interval_seconds", 0),
    ],
)
def test_positive_settings_reject_zero(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value}, _env_file=None)


def test_blank_redis_key_prefix_is_rejected() -> None:
    with pytest.raises(ValidationError, match="REDIS_KEY_PREFIX"):
        Settings(redis_key_prefix=" : ", _env_file=None)


def test_legacy_postgresql_url_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"mysql\+asyncmy"):
        Settings(
            database_url=SecretStr(
                "postgresql+asyncpg://fitweek:secret@127.0.0.1/fitweek_test"
            ),
            _env_file=None,
        )
