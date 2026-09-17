"""Typed tool metadata; generic result metadata deliberately excludes payloads."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.tools.enums import (
    CircuitState,
    ToolCaller,
    ToolDegradationMode,
    ToolErrorCategory,
    ToolId,
    ToolInvocationStatus,
    ToolSideEffectClass,
)


class ToolDescriptor(BaseModel):
    """Static registration contract; it is never supplied by an API caller."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    tool_id: ToolId
    version: str = Field(min_length=1)
    side_effect_class: ToolSideEffectClass
    request_model: type[BaseModel]
    response_model: type[BaseModel]
    default_timeout_ms: int = Field(gt=0, le=60_000)
    request_deadline_budget_ms: int = Field(gt=0, le=120_000)
    max_attempts: int = Field(ge=1, le=3)
    circuit_breaker_enabled: bool
    bulkhead_limit: int = Field(gt=0, le=128)
    supports_degradation: bool
    requires_idempotency_key: bool
    allowed_callers: frozenset[ToolCaller]
    provider_name: str = "internal"
    operation_class: str = "default"

    @field_validator("allowed_callers")
    @classmethod
    def require_caller(cls, callers: frozenset[ToolCaller]) -> frozenset[ToolCaller]:
        if not callers:
            raise ValueError("A tool descriptor requires at least one caller.")
        return callers


class ToolInvocationContext(BaseModel):
    """Privacy-minimal invocation metadata, never a copy of the tool request."""

    model_config = ConfigDict(frozen=True)
    invocation_id: UUID
    correlation_id: UUID
    user_id: UUID
    caller: ToolCaller
    tool_id: ToolId
    tool_version: str
    deadline_at: datetime
    created_at: datetime
    run_id: UUID | None = None
    step_id: UUID | None = None
    request_id: str | None = None
    idempotency_key: str | None = None

    @field_validator("deadline_at", "created_at")
    @classmethod
    def require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Tool timestamps must be timezone-aware.")
        return value


class ToolInvocationResult(BaseModel):
    """Safe common metadata. The typed response travels separately in memory."""

    model_config = ConfigDict(frozen=True)
    invocation_id: UUID
    tool_id: ToolId
    tool_version: str
    status: ToolInvocationStatus
    degradation_mode: ToolDegradationMode = ToolDegradationMode.NONE
    attempt_count: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    response_reference: str | None = None
    error_category: ToolErrorCategory | None = None
    error_code: str | None = None
    circuit_state_before: CircuitState
    circuit_state_after: CircuitState
    created_at: datetime


class TypedToolResult(BaseModel):
    """Gateway outcome paired with a validated, typed adapter response."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    result: ToolInvocationResult
    response: BaseModel | None = None
