"""Immutable request, response, trace, and gateway result values."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.common import require_non_blank, require_utc_datetime
from app.domain.context.enums import ContextDegradedMode
from app.domain.model_gateway.enums import (
    FallbackType,
    ModelAttemptOutcome,
    ModelErrorCode,
    ProviderRole,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelRequest:
    request_id: UUID
    model: str
    system_prompt: str
    user_prompt: str
    response_schema_name: str
    temperature: float
    max_output_tokens: int
    timeout_seconds: float
    metadata: Mapping[str, str]

    def __post_init__(self) -> None:
        require_non_blank(self.model, "model")
        require_non_blank(self.system_prompt, "system_prompt")
        require_non_blank(self.user_prompt, "user_prompt")
        require_non_blank(self.response_schema_name, "response_schema_name")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if any(
            not key or not isinstance(value, str)
            for key, value in self.metadata.items()
        ):
            raise ValueError("metadata must contain non-empty string keys and values")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelProviderResponse:
    provider_name: str
    provider_version: str
    model: str
    raw_text: str
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    provider_request_id: str | None
    latency_ms: float


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelTraceContext:
    user_id: UUID
    agent_name: str
    prompt_name: str
    prompt_version: str
    prompt_sha256: str
    input_fingerprint: str
    context_snapshot_reference_id: UUID | None = None
    context_fingerprint: str | None = None
    context_contract_version: str | None = None
    context_degraded_mode: ContextDegradedMode = ContextDegradedMode.NONE


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCallTrace:
    id: UUID
    request_id: UUID
    user_id: UUID
    agent_name: str
    prompt_name: str
    prompt_version: str
    prompt_sha256: str
    provider_name: str
    provider_version: str
    provider_role: ProviderRole
    model: str
    attempt_no: int
    outcome: ModelAttemptOutcome
    error_code: ModelErrorCode | None
    error_message: str | None
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    fallback_used: bool
    fallback_type: FallbackType | None
    input_fingerprint: str
    scope_guard_blocked: bool
    created_at: datetime
    context_snapshot_reference_id: UUID | None = None
    context_fingerprint: str | None = None
    context_contract_version: str | None = None
    context_degraded_mode: ContextDegradedMode = ContextDegradedMode.NONE

    def __post_init__(self) -> None:
        require_utc_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelGatewayResult[T]:
    request_id: UUID
    value: T
    provider_name: str
    provider_version: str
    model: str
    fallback_used: bool
    fallback_type: FallbackType | None
    provider_attempts: int
