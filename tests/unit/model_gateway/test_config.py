"""Phase 3A model-provider configuration contracts."""

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings

pytestmark = pytest.mark.phase_3a


def test_default_gateway_configuration_is_explicit_and_safe() -> None:
    settings = Settings(_env_file=None)

    assert settings.model_gateway_enabled is True
    assert settings.model_primary_provider == "scripted-fake"
    assert settings.model_backup_provider == "template-fallback"
    assert settings.model_max_attempts == 3
    assert settings.model_trace_include_user_content is False


def test_http_provider_requires_url_and_secret_key() -> None:
    with pytest.raises(ValidationError, match="BASE_URL"):
        Settings(model_primary_provider="http", _env_file=None)
    with pytest.raises(ValidationError, match="API_KEY"):
        Settings(
            model_primary_provider="http",
            model_primary_base_url=SecretStr("http://127.0.0.1:9999"),
            _env_file=None,
        )


def test_http_provider_masks_key_and_base_url() -> None:
    key = "phase-3a-secret-value"
    settings = Settings(
        model_primary_provider="http",
        model_primary_base_url=SecretStr("http://127.0.0.1:9999"),
        model_primary_api_key=SecretStr(key),
        _env_file=None,
    )

    assert key not in repr(settings)
    assert "127.0.0.1:9999" not in repr(settings)
    assert "**********" in repr(settings)


def test_embedded_base_url_credentials_are_rejected() -> None:
    with pytest.raises(ValidationError, match="embed credentials"):
        Settings(
            model_primary_provider="http",
            model_primary_base_url=SecretStr("http://user:pass@127.0.0.1:9999"),
            model_primary_api_key=SecretStr("key"),
            _env_file=None,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_request_timeout_seconds", 0),
        ("model_connect_timeout_seconds", 0),
        ("model_max_attempts", 5),
        ("model_max_concurrency", 0),
        ("model_rate_limit_per_minute", 0),
        ("model_max_response_bytes", 100),
    ],
)
def test_invalid_gateway_bounds_fail_fast(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value}, _env_file=None)


def test_provider_configuration_never_silently_selects_fake() -> None:
    with pytest.raises(ValidationError):
        Settings(model_primary_provider="unknown", _env_file=None)
    with pytest.raises(ValidationError, match="cannot be the primary"):
        Settings(model_primary_provider="template-fallback", _env_file=None)
