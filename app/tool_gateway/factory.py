"""Static Phase 8A registration; no module scanning or runtime discovery."""

from typing import cast

from pydantic import BaseModel

from app.domain.calendar_operations.protocols import CalendarWriteProvider
from app.domain.calendar_read.protocols import CalendarReadProvider
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.tools.enums import ToolCaller, ToolId, ToolSideEffectClass
from app.domain.tools.models import ToolDescriptor
from app.domain.tools.protocols import ToolAdapter
from app.memory.candidate_service import MemoryCandidateService
from app.observability.facade import ObservabilityFacade
from app.reliability.clock import Clock, SystemClock
from app.reliability.sleeper import AsyncioSleeper
from app.tool_adapters.basic import (
    CalendarCommitAdapter,
    CalendarReadAdapter,
    CatalogSearchAdapter,
    IcsExportAdapter,
    MemoryCandidateCreateAdapter,
    RecoverySpacingAdapter,
    SessionDurationAdapter,
)
from app.tool_adapters.contracts import (
    CalendarCommitRequest,
    CalendarCommitResponse,
    CalendarFreeBusyRequest,
    CalendarFreeBusyResponse,
    ExerciseCatalogSearchRequest,
    ExerciseCatalogSearchResponse,
    IcsExportRequest,
    IcsExportResponse,
    MemoryCandidateCreateRequest,
    MemoryCandidateCreateResponse,
    RecoverySpacingRequest,
    RecoverySpacingResponse,
    SessionDurationRequest,
    SessionDurationResponse,
)
from app.tool_gateway.bulkhead import BulkheadManager
from app.tool_gateway.circuit_breaker import CircuitBreaker
from app.tool_gateway.gateway import ToolGateway
from app.tool_gateway.registry import ToolRegistry
from app.tool_gateway.retry import RetryBudgetRegistry


def _descriptor(
    tool_id: ToolId,
    request: type[BaseModel],
    response: type[BaseModel],
    effect: ToolSideEffectClass,
    callers: frozenset[ToolCaller],
    *,
    timeout: int,
    deadline: int,
    attempts: int,
    bulkhead: int,
    degradation: bool = False,
    idempotency: bool = False,
    provider: str = "internal",
    operation: str = "default",
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=tool_id,
        version="phase-8a-v1",
        side_effect_class=effect,
        request_model=request,
        response_model=response,
        default_timeout_ms=timeout,
        request_deadline_budget_ms=deadline,
        max_attempts=attempts,
        circuit_breaker_enabled=provider != "internal",
        bulkhead_limit=bulkhead,
        supports_degradation=degradation,
        requires_idempotency_key=idempotency,
        allowed_callers=callers,
        provider_name=provider,
        operation_class=operation,
    )


def build_tool_gateway(
    exercise_ids: tuple[str, ...] = (),
    *,
    calendar_read_provider: CalendarReadProvider | None = None,
    calendar_write_provider: CalendarWriteProvider | None = None,
    calendar_read_timeout_ms: int = 3000,
    calendar_read_attempts: int = 2,
    calendar_write_timeout_ms: int = 3000,
    calendar_write_attempts: int = 3,
    calendar_read_bulkhead_limit: int = 10,
    circuit_failure_threshold: int = 5,
    circuit_failure_window_seconds: int = 60,
    circuit_open_duration_seconds: int = 30,
    retry_budget_maximum_attempts: int = 12,
    clock: Clock | None = None,
    exercise_repository: ExerciseRepository | None = None,
    memory_candidate_service: MemoryCandidateService | None = None,
    observability: ObservabilityFacade | None = None,
) -> ToolGateway:
    registry = ToolRegistry()
    registrations = (
        (
            _descriptor(
                ToolId.EXERCISE_CATALOG_SEARCH,
                ExerciseCatalogSearchRequest,
                ExerciseCatalogSearchResponse,
                ToolSideEffectClass.READ_ONLY,
                frozenset(
                    {
                        ToolCaller.PLAN_GENERATION_APPLICATION,
                        ToolCaller.SESSION_DESIGN_APPLICATION,
                        ToolCaller.RECOVERY_APPLICATION,
                    }
                ),
                timeout=1000,
                deadline=2000,
                attempts=1,
                bulkhead=32,
            ),
            CatalogSearchAdapter(exercise_repository),
        ),
        (
            _descriptor(
                ToolId.SESSION_DURATION_CALCULATOR,
                SessionDurationRequest,
                SessionDurationResponse,
                ToolSideEffectClass.PURE,
                frozenset(
                    {
                        ToolCaller.SESSION_DESIGN_APPLICATION,
                        ToolCaller.RECOVERY_APPLICATION,
                    }
                ),
                timeout=500,
                deadline=1000,
                attempts=1,
                bulkhead=32,
            ),
            SessionDurationAdapter(),
        ),
        (
            _descriptor(
                ToolId.CALENDAR_FREE_BUSY,
                CalendarFreeBusyRequest,
                CalendarFreeBusyResponse,
                ToolSideEffectClass.READ_ONLY,
                frozenset(
                    {ToolCaller.SCHEDULE_APPLICATION, ToolCaller.RECOVERY_APPLICATION}
                ),
                timeout=calendar_read_timeout_ms,
                deadline=calendar_read_timeout_ms * calendar_read_attempts + 1000,
                attempts=calendar_read_attempts,
                bulkhead=calendar_read_bulkhead_limit,
                degradation=True,
                provider="calendar",
                operation="read",
            ),
            CalendarReadAdapter(calendar_read_provider),
        ),
        (
            _descriptor(
                ToolId.RECOVERY_SPACING_VALIDATOR,
                RecoverySpacingRequest,
                RecoverySpacingResponse,
                ToolSideEffectClass.PURE,
                frozenset({ToolCaller.RECOVERY_APPLICATION}),
                timeout=500,
                deadline=1000,
                attempts=1,
                bulkhead=32,
            ),
            RecoverySpacingAdapter(),
        ),
        (
            _descriptor(
                ToolId.ICS_EXPORT,
                IcsExportRequest,
                IcsExportResponse,
                ToolSideEffectClass.FILE_WRITE,
                frozenset({ToolCaller.ICS_EXPORT_SERVICE}),
                timeout=1000,
                deadline=2000,
                attempts=1,
                bulkhead=8,
                idempotency=True,
            ),
            IcsExportAdapter(),
        ),
        (
            _descriptor(
                ToolId.CALENDAR_COMMIT,
                CalendarCommitRequest,
                CalendarCommitResponse,
                ToolSideEffectClass.EXTERNAL_WRITE,
                frozenset({ToolCaller.CALENDAR_EXECUTOR}),
                timeout=calendar_write_timeout_ms,
                deadline=(calendar_write_timeout_ms * calendar_write_attempts + 1000),
                attempts=calendar_write_attempts,
                bulkhead=4,
                degradation=True,
                idempotency=True,
                provider="calendar",
                operation="write",
            ),
            CalendarCommitAdapter(calendar_write_provider),
        ),
        (
            _descriptor(
                ToolId.MEMORY_CANDIDATE_CREATE,
                MemoryCandidateCreateRequest,
                MemoryCandidateCreateResponse,
                ToolSideEffectClass.INTERNAL_WRITE,
                frozenset({ToolCaller.MEMORY_COMMITTER}),
                timeout=1000,
                deadline=2000,
                attempts=1,
                bulkhead=16,
                degradation=True,
                idempotency=True,
            ),
            MemoryCandidateCreateAdapter(memory_candidate_service),
        ),
    )
    for descriptor, adapter in registrations:
        registry.register(descriptor, cast(ToolAdapter[BaseModel, BaseModel], adapter))
    active_clock = clock or SystemClock()
    return ToolGateway(
        registry=registry,
        clock=active_clock,
        sleeper=AsyncioSleeper(),
        circuits=CircuitBreaker(
            active_clock,
            failure_threshold=circuit_failure_threshold,
            failure_window_seconds=circuit_failure_window_seconds,
            open_duration_seconds=circuit_open_duration_seconds,
        ),
        bulkheads=BulkheadManager(),
        budgets=RetryBudgetRegistry(
            maximum_total_attempts=retry_budget_maximum_attempts
        ),
        observability=observability,
    )
