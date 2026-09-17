"""Privacy-minimal context shared by traces and structured logs."""

from contextvars import ContextVar, Token
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ObservabilityContext(BaseModel):
    """References only; never user, prompt, Memory, or Calendar content."""

    model_config = ConfigDict(frozen=True)

    correlation_id: UUID
    request_id: str | None = Field(default=None, max_length=128)
    run_id: UUID | None = None
    step_id: UUID | None = None
    trace_id: str | None = None
    span_id: str | None = None
    operation_name: str = Field(min_length=1, max_length=128)
    component: str = Field(min_length=1, max_length=64)


_CURRENT_CONTEXT: ContextVar[ObservabilityContext | None] = ContextVar(
    "fitweek_observability_context",
    default=None,
)


def current_observability_context() -> ObservabilityContext | None:
    """Return the task-local safe context, never request content."""

    return _CURRENT_CONTEXT.get()


def set_observability_context(
    context: ObservabilityContext,
) -> Token[ObservabilityContext | None]:
    return _CURRENT_CONTEXT.set(context)


def reset_observability_context(token: Token[ObservabilityContext | None]) -> None:
    _CURRENT_CONTEXT.reset(token)
