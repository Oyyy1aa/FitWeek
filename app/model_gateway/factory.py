"""Explicit settings-driven provider and gateway composition."""

import json

from app.config import Settings
from app.domain.model_gateway.protocols import ModelProvider
from app.model_gateway.fake_provider import ScriptedFakeProvider
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.http_provider import OpenAICompatibleHttpProvider
from app.model_gateway.limiter import ProcessLocalModelLimiter
from app.model_gateway.retry_policy import ModelRetryPolicy
from app.observability.facade import ObservabilityFacade

_SAFE_FAKE_OUTPUT = json.dumps(
    {
        "weekly_frequency": None,
        "max_session_minutes": None,
        "goals": [],
        "hard_constraints": [],
        "soft_preferences": [],
        "temporary_constraints": [],
        "equipment": [],
        "locations": [],
        "scope_status": "NEEDS_REVIEW",
        "missing_fields": [
            "weekly_frequency",
            "max_session_minutes",
            "primary_goal",
            "available_equipment",
            "available_location",
        ],
        "memory_candidates": [],
        "explanation_summary": (
            "The scripted local provider returned a review-only contract result."
        ),
    },
    separators=(",", ":"),
)


def build_model_gateway(
    settings: Settings,
    *,
    observability: ObservabilityFacade | None = None,
) -> ModelGateway:
    """Build only providers explicitly selected in settings."""

    primary = _build_provider(settings, role="primary")
    backup = (
        None
        if settings.model_backup_provider == "template-fallback"
        else _build_provider(settings, role="backup")
    )
    return ModelGateway(
        primary=primary,
        primary_model=settings.model_primary_model,
        backup=backup,
        backup_model=settings.model_backup_model,
        retry_policy=ModelRetryPolicy(max_attempts=settings.model_max_attempts),
        limiter=ProcessLocalModelLimiter(
            max_concurrency=settings.model_max_concurrency,
            rate_per_minute=settings.model_rate_limit_per_minute,
            acquire_timeout_seconds=settings.model_request_timeout_seconds,
        ),
        observability=observability,
    )


def _build_provider(settings: Settings, *, role: str) -> ModelProvider:
    if role == "primary":
        provider = settings.model_primary_provider
        base_url = settings.model_primary_base_url
        api_key = settings.model_primary_api_key
        version = settings.model_primary_provider_version
    else:
        provider = settings.model_backup_provider
        base_url = settings.model_backup_base_url
        api_key = settings.model_backup_api_key
        version = settings.model_backup_provider_version
    if provider == "scripted-fake":
        return ScriptedFakeProvider(
            provider_name="scripted-fake",
            provider_version=version,
            default_raw_text=_SAFE_FAKE_OUTPUT,
        )
    if provider == "http":
        if base_url is None or api_key is None:
            raise ValueError(f"{role} HTTP provider configuration is incomplete")
        return OpenAICompatibleHttpProvider(
            base_url=base_url.get_secret_value(),
            api_key=api_key.get_secret_value(),
            provider_version=version,
            connect_timeout_seconds=settings.model_connect_timeout_seconds,
            max_response_bytes=settings.model_max_response_bytes,
        )
    raise ValueError(f"{provider!r} cannot be built as a callable provider")
