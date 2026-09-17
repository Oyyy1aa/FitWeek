"""Bounded redacted per-attempt Tool traces."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.tools.enums import (
    CircuitState,
    ToolDegradationMode,
    ToolErrorCategory,
    ToolId,
    ToolInvocationStatus,
)


@dataclass(frozen=True, slots=True)
class ToolCallTrace:
    invocation_id: UUID
    correlation_id: UUID
    user_id: UUID
    caller: str
    tool_id: ToolId
    tool_version: str
    provider_name: str
    provider_version: str
    attempt_no: int
    status: str
    error_category: ToolErrorCategory | None
    error_code: str | None
    latency_ms: float
    circuit_before: CircuitState
    circuit_after: CircuitState
    degradation_mode: ToolDegradationMode
    created_at: datetime
    run_id: UUID | None = None
    step_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ToolInvocationSummary:
    invocation_id: UUID
    correlation_id: UUID
    user_id: UUID
    caller: str
    tool_id: ToolId
    tool_version: str
    attempt_count: int
    status: ToolInvocationStatus
    error_category: ToolErrorCategory | None
    error_code: str | None
    latency_ms: float
    circuit_before: CircuitState
    circuit_after: CircuitState
    degradation_mode: ToolDegradationMode
    created_at: datetime
    run_id: UUID | None = None
    step_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ReliabilityCompatibilityTrace:
    """Payload-free trace for bounded legacy reliability bridges."""

    correlation_id: UUID
    user_id: UUID
    component: str
    attempt_no: int
    status: str
    error_category: ToolErrorCategory | None
    error_code: str | None
    degradation_mode: ToolDegradationMode
    created_at: datetime


class ToolTraceStore:
    def __init__(self, limit: int = 1000) -> None:
        self._items: deque[ToolCallTrace] = deque(maxlen=limit)
        self._summaries: deque[ToolInvocationSummary] = deque(maxlen=limit)
        self._compatibility: deque[ReliabilityCompatibilityTrace] = deque(maxlen=limit)

    def add(self, trace: ToolCallTrace) -> None:
        self._items.append(trace)

    def add_summary(self, summary: ToolInvocationSummary) -> None:
        self._summaries.append(summary)

    def add_compatibility(self, trace: ReliabilityCompatibilityTrace) -> None:
        self._compatibility.append(trace)

    def by_correlation(
        self, correlation_id: UUID, limit: int = 100
    ) -> tuple[ToolCallTrace, ...]:
        return tuple(
            item for item in self._items if item.correlation_id == correlation_id
        )[-limit:]

    def by_correlation_for_user(
        self, user_id: UUID, correlation_id: UUID, limit: int = 100
    ) -> tuple[ToolCallTrace, ...]:
        return tuple(
            item
            for item in self._items
            if item.user_id == user_id and item.correlation_id == correlation_id
        )[-limit:]

    def summaries_by_correlation_for_user(
        self, user_id: UUID, correlation_id: UUID, limit: int = 100
    ) -> tuple[ToolInvocationSummary, ...]:
        return tuple(
            item
            for item in self._summaries
            if item.user_id == user_id and item.correlation_id == correlation_id
        )[-limit:]

    def summaries_for_user(
        self, user_id: UUID, *, limit: int = 100
    ) -> tuple[ToolInvocationSummary, ...]:
        return tuple(item for item in self._summaries if item.user_id == user_id)[
            -limit:
        ]

    def list_for_user(
        self, user_id: UUID, *, limit: int = 100
    ) -> tuple[ToolCallTrace, ...]:
        """Return a bounded, user-isolated view without exposing stored payloads."""

        return tuple(item for item in self._items if item.user_id == user_id)[-limit:]

    def compatibility_for_user(
        self,
        user_id: UUID,
        *,
        correlation_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[ReliabilityCompatibilityTrace, ...]:
        return tuple(
            item
            for item in self._compatibility
            if item.user_id == user_id
            and (correlation_id is None or item.correlation_id == correlation_id)
        )[-limit:]
