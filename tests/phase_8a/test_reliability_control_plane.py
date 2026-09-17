"""Deterministic fault-closure tests for the in-process reliability controls."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from app.domain.tools.enums import (
    CircuitState,
    ToolCaller,
    ToolDegradationMode,
    ToolErrorCategory,
    ToolId,
    ToolInvocationStatus,
    ToolSideEffectClass,
)
from app.domain.tools.errors import ToolError
from app.domain.tools.models import ToolDescriptor, ToolInvocationContext
from app.reliability.clock import MutableClock, SystemClock
from app.reliability.sleeper import RecordingSleeper
from app.tool_gateway.bulkhead import BulkheadManager
from app.tool_gateway.circuit_breaker import CircuitBreaker, CircuitKey
from app.tool_gateway.gateway import ToolGateway
from app.tool_gateway.metrics import ToolMetrics
from app.tool_gateway.registry import ToolRegistry
from app.tool_gateway.retry import RetryBudgetRegistry
from app.tool_gateway.traces import ToolCallTrace, ToolTraceStore

pytestmark = pytest.mark.phase_8a


class Request(BaseModel):
    value: str = "value"


class Response(BaseModel):
    value: str


class RecordingAdapter:
    def __init__(
        self,
        failures: list[ToolErrorCategory] | None = None,
        *,
        delay_seconds: float = 0,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.failures = list(failures or [])
        self.delay_seconds = delay_seconds
        self.gate = gate
        self.started = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def invoke_once(
        self, context: ToolInvocationContext, request: Request
    ) -> Response:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            if self.failures:
                category = self.failures.pop(0)
                raise ToolError(category, f"TEST_{category.value}")
            return Response(value=request.value)
        finally:
            self.active -= 1

    async def close(self) -> None:
        return None


def _descriptor(
    tool_id: ToolId = ToolId.CALENDAR_FREE_BUSY,
    *,
    attempts: int = 2,
    circuit: bool = True,
    bulkhead: int = 2,
    timeout_ms: int = 1000,
    effect: ToolSideEffectClass = ToolSideEffectClass.READ_ONLY,
    callers: frozenset[ToolCaller] | None = None,
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=tool_id,
        version="test-v1",
        side_effect_class=effect,
        request_model=Request,
        response_model=Response,
        default_timeout_ms=timeout_ms,
        request_deadline_budget_ms=2000,
        max_attempts=attempts,
        circuit_breaker_enabled=circuit,
        bulkhead_limit=bulkhead,
        supports_degradation=True,
        requires_idempotency_key=False,
        allowed_callers=callers
        or frozenset(
            {
                ToolCaller.SCHEDULE_APPLICATION,
                ToolCaller.CALENDAR_EXECUTOR,
            }
        ),
        provider_name="calendar",
        operation_class=("write" if tool_id is ToolId.CALENDAR_COMMIT else "read"),
    )


def _context(
    clock: MutableClock | SystemClock,
    *,
    tool_id: ToolId = ToolId.CALENDAR_FREE_BUSY,
    correlation_id: UUID | None = None,
    user_id: UUID | None = None,
    caller: ToolCaller = ToolCaller.SCHEDULE_APPLICATION,
    deadline_ms: int = 2000,
) -> ToolInvocationContext:
    now = clock.now()
    return ToolInvocationContext(
        invocation_id=uuid4(),
        correlation_id=correlation_id or uuid4(),
        user_id=user_id or uuid4(),
        caller=caller,
        tool_id=tool_id,
        tool_version="test-v1",
        deadline_at=now + timedelta(milliseconds=deadline_ms),
        created_at=now,
    )


def _gateway(
    clock: MutableClock | SystemClock,
    registrations: tuple[tuple[ToolDescriptor, RecordingAdapter], ...],
    *,
    threshold: int = 5,
    open_seconds: int = 30,
    budgets: RetryBudgetRegistry | None = None,
    traces: ToolTraceStore | None = None,
    metrics: ToolMetrics | None = None,
) -> ToolGateway:
    registry = ToolRegistry()
    for descriptor, adapter in registrations:
        registry.register(descriptor, adapter)
    return ToolGateway(
        registry=registry,
        clock=clock,
        sleeper=RecordingSleeper(),
        circuits=CircuitBreaker(
            clock,
            failure_threshold=threshold,
            open_duration_seconds=open_seconds,
        ),
        bulkheads=BulkheadManager(),
        budgets=budgets,
        traces=traces,
        metrics=metrics,
    )


def _key(tool_id: ToolId = ToolId.CALENDAR_FREE_BUSY) -> CircuitKey:
    return CircuitKey(
        tool_id.value,
        "test-v1",
        "calendar",
        "write" if tool_id is ToolId.CALENDAR_COMMIT else "read",
    )


@pytest.mark.asyncio
async def test_five_failed_invocations_open_circuit_and_sixth_skips_adapter() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter([ToolErrorCategory.UPSTREAM_5XX] * 10)
    gateway = _gateway(clock, ((_descriptor(), adapter),))
    results = [await gateway.invoke(_context(clock), Request()) for _ in range(5)]
    assert all(
        item.result.circuit_state_after is CircuitState.CLOSED for item in results[:4]
    )
    assert results[4].result.circuit_state_after is CircuitState.OPEN
    assert adapter.calls == 10
    blocked = await gateway.invoke(_context(clock), Request())
    assert blocked.result.error_code == "TOOL_CIRCUIT_OPEN"
    assert adapter.calls == 10


@pytest.mark.asyncio
async def test_circuit_counts_final_invocation_not_internal_attempts() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter([ToolErrorCategory.CONNECTION] * 2)
    gateway = _gateway(clock, ((_descriptor(), adapter),))
    result = await gateway.invoke(_context(clock), Request())
    assert result.result.attempt_count == 2
    assert adapter.calls == 2
    assert await gateway.circuits.failure_count(_key()) == 1


@pytest.mark.asyncio
async def test_open_circuit_remains_open_before_cooldown() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter([ToolErrorCategory.UPSTREAM_5XX] * 2)
    gateway = _gateway(clock, ((_descriptor(), adapter),), threshold=1)
    await gateway.invoke(_context(clock), Request())
    clock.advance(timedelta(seconds=29))
    before = adapter.calls
    result = await gateway.invoke(_context(clock), Request())
    assert result.result.circuit_state_before is CircuitState.OPEN
    assert adapter.calls == before


@pytest.mark.asyncio
async def test_half_open_success_closes_circuit() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter([ToolErrorCategory.UPSTREAM_5XX] * 2)
    gateway = _gateway(clock, ((_descriptor(), adapter),), threshold=1)
    await gateway.invoke(_context(clock), Request())
    clock.advance(timedelta(seconds=30))
    adapter.failures.clear()
    result = await gateway.invoke(_context(clock), Request())
    assert result.result.circuit_state_before is CircuitState.HALF_OPEN
    assert result.result.circuit_state_after is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_half_open_failure_reopens_circuit() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter([ToolErrorCategory.UPSTREAM_5XX] * 4)
    gateway = _gateway(clock, ((_descriptor(), adapter),), threshold=1)
    await gateway.invoke(_context(clock), Request())
    clock.advance(timedelta(seconds=30))
    result = await gateway.invoke(_context(clock), Request())
    assert result.result.circuit_state_before is CircuitState.HALF_OPEN
    assert result.result.circuit_state_after is CircuitState.OPEN


@pytest.mark.asyncio
async def test_half_open_allows_only_one_concurrent_probe() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter([ToolErrorCategory.UPSTREAM_5XX] * 2)
    gateway = _gateway(clock, ((_descriptor(), adapter),), threshold=1)
    await gateway.invoke(_context(clock), Request())
    clock.advance(timedelta(seconds=30))
    adapter.failures.clear()
    adapter.started = asyncio.Event()
    adapter.gate = asyncio.Event()
    probe = asyncio.create_task(gateway.invoke(_context(clock), Request()))
    await adapter.started.wait()
    calls = adapter.calls
    rejected = await gateway.invoke(_context(clock), Request())
    assert rejected.result.error_code == "TOOL_CIRCUIT_OPEN"
    assert adapter.calls == calls
    adapter.gate.set()
    assert (await probe).result.status is ToolInvocationStatus.SUCCEEDED


@pytest.mark.parametrize(
    "category",
    [
        ToolErrorCategory.INVALID_REQUEST,
        ToolErrorCategory.AUTHENTICATION,
        ToolErrorCategory.AUTHORIZATION,
        ToolErrorCategory.PERMISSION,
        ToolErrorCategory.BUSINESS_REJECTED,
        ToolErrorCategory.IDEMPOTENCY_CONFLICT,
    ],
)
@pytest.mark.asyncio
async def test_business_and_client_errors_do_not_increment_circuit(
    category: ToolErrorCategory,
) -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter([category])
    descriptor = _descriptor(attempts=1)
    gateway = _gateway(clock, ((descriptor, adapter),), threshold=1)
    result = await gateway.invoke(_context(clock), Request())
    assert result.result.circuit_state_after is CircuitState.CLOSED
    assert await gateway.circuits.failure_count(_key()) == 0


@pytest.mark.asyncio
async def test_permission_rejection_never_reaches_circuit_or_adapter() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter()
    gateway = _gateway(clock, ((_descriptor(), adapter),))
    result = await gateway.invoke(
        _context(clock, caller=ToolCaller.PROFILE_APPLICATION), Request()
    )
    assert result.result.error_code == "TOOL_PERMISSION_DENIED"
    assert adapter.calls == 0
    assert await gateway.circuits.failure_count(_key()) == 0


@pytest.mark.asyncio
async def test_read_and_write_circuit_keys_are_isolated() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    read = RecordingAdapter([ToolErrorCategory.CONNECTION])
    write = RecordingAdapter()
    gateway = _gateway(
        clock,
        (
            (_descriptor(attempts=1), read),
            (
                _descriptor(
                    ToolId.CALENDAR_COMMIT,
                    attempts=1,
                    callers=frozenset({ToolCaller.CALENDAR_EXECUTOR}),
                ),
                write,
            ),
        ),
        threshold=1,
    )
    await gateway.invoke(_context(clock), Request())
    result = await gateway.invoke(
        _context(
            clock,
            tool_id=ToolId.CALENDAR_COMMIT,
            caller=ToolCaller.CALENDAR_EXECUTOR,
        ),
        Request(),
    )
    assert result.result.status is ToolInvocationStatus.SUCCEEDED
    assert await gateway.circuits.state(_key()) is CircuitState.OPEN
    assert await gateway.circuits.state(_key(ToolId.CALENDAR_COMMIT)) is (
        CircuitState.CLOSED
    )


@pytest.mark.asyncio
async def test_bulkhead_limits_concurrency_and_releases_all_permits() -> None:
    clock = SystemClock()
    adapter = RecordingAdapter(delay_seconds=0.02)
    gateway = _gateway(clock, ((_descriptor(bulkhead=2), adapter),))
    results = await asyncio.gather(
        *(gateway.invoke(_context(clock), Request()) for _ in range(5))
    )
    assert all(item.result.status is ToolInvocationStatus.SUCCEEDED for item in results)
    assert adapter.max_active <= 2
    assert (
        gateway.bulkheads.active_count((ToolId.CALENDAR_FREE_BUSY.value, "test-v1"))
        == 0
    )


@pytest.mark.asyncio
async def test_bulkheads_are_independent_between_tools() -> None:
    clock = SystemClock()
    gate = asyncio.Event()
    read = RecordingAdapter(gate=gate)
    export = RecordingAdapter()
    gateway = _gateway(
        clock,
        (
            (_descriptor(bulkhead=1), read),
            (
                _descriptor(
                    ToolId.ICS_EXPORT,
                    attempts=1,
                    circuit=False,
                    effect=ToolSideEffectClass.FILE_WRITE,
                    callers=frozenset({ToolCaller.ICS_EXPORT_SERVICE}),
                ),
                export,
            ),
        ),
    )
    blocked = asyncio.create_task(gateway.invoke(_context(clock), Request()))
    await read.started.wait()
    other = await gateway.invoke(
        _context(
            clock,
            tool_id=ToolId.ICS_EXPORT,
            caller=ToolCaller.ICS_EXPORT_SERVICE,
        ),
        Request(),
    )
    assert other.result.status is ToolInvocationStatus.SUCCEEDED
    gate.set()
    await blocked


@pytest.mark.asyncio
async def test_bulkhead_releases_permit_after_adapter_exception() -> None:
    clock = SystemClock()
    adapter = RecordingAdapter([ToolErrorCategory.BUSINESS_REJECTED])
    gateway = _gateway(clock, ((_descriptor(attempts=1, bulkhead=1), adapter),))
    await gateway.invoke(_context(clock), Request())
    assert (
        gateway.bulkheads.active_count((ToolId.CALENDAR_FREE_BUSY.value, "test-v1"))
        == 0
    )


@pytest.mark.asyncio
async def test_bulkhead_releases_permit_after_cancellation() -> None:
    clock = SystemClock()
    gate = asyncio.Event()
    adapter = RecordingAdapter(gate=gate)
    gateway = _gateway(clock, ((_descriptor(bulkhead=1), adapter),))
    task = asyncio.create_task(gateway.invoke(_context(clock), Request()))
    await adapter.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.active == 0
    assert (
        gateway.bulkheads.active_count((ToolId.CALENDAR_FREE_BUSY.value, "test-v1"))
        == 0
    )


@pytest.mark.asyncio
async def test_bulkhead_wait_is_bounded_by_deadline() -> None:
    clock = SystemClock()
    gate = asyncio.Event()
    adapter = RecordingAdapter(gate=gate)
    gateway = _gateway(clock, ((_descriptor(bulkhead=1), adapter),))
    first = asyncio.create_task(gateway.invoke(_context(clock), Request()))
    await adapter.started.wait()
    rejected = await gateway.invoke(_context(clock, deadline_ms=5), Request())
    assert rejected.result.error_code == "TOOL_BULKHEAD_REJECTED"
    gate.set()
    await first
    assert gateway.metrics.snapshot()["counts"]["tool_bulkhead_rejections_total"] == 1


@pytest.mark.asyncio
async def test_expired_deadline_skips_adapter() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter()
    gateway = _gateway(clock, ((_descriptor(), adapter),))
    result = await gateway.invoke(_context(clock, deadline_ms=0), Request())
    assert result.result.error_code == "TOOL_DEADLINE_EXCEEDED"
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_total_deadline_stops_slow_adapter_and_releases_permit() -> None:
    clock = SystemClock()
    adapter = RecordingAdapter(delay_seconds=0.05)
    gateway = _gateway(
        clock, ((_descriptor(timeout_ms=100, attempts=2, bulkhead=1), adapter),)
    )
    result = await gateway.invoke(_context(clock, deadline_ms=10), Request())
    assert result.result.error_code == "TOOL_DEADLINE_EXCEEDED"
    assert result.result.attempt_count == 1
    assert adapter.calls == 1 and adapter.active == 0
    assert (
        gateway.bulkheads.active_count((ToolId.CALENDAR_FREE_BUSY.value, "test-v1"))
        == 0
    )


@pytest.mark.asyncio
async def test_attempt_timeout_retries_within_larger_deadline() -> None:
    clock = SystemClock()
    adapter = RecordingAdapter(delay_seconds=0.02)
    gateway = _gateway(clock, ((_descriptor(timeout_ms=5, attempts=2), adapter),))
    result = await gateway.invoke(_context(clock, deadline_ms=500), Request())
    assert result.result.error_code == "TOOL_ATTEMPT_TIMEOUT"
    assert result.result.attempt_count == 2
    assert adapter.calls == 2


@pytest.mark.asyncio
async def test_retry_backoff_is_owned_and_recorded_by_gateway() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter([ToolErrorCategory.CONNECTION])
    gateway = _gateway(clock, ((_descriptor(), adapter),))
    result = await gateway.invoke(_context(clock), Request())
    assert result.result.status is ToolInvocationStatus.SUCCEEDED
    assert gateway.sleeper.calls == [100]
    assert adapter.calls == 2


@pytest.mark.asyncio
async def test_retry_budget_is_shared_across_tools_for_one_correlation() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    correlation = uuid4()
    first = RecordingAdapter([ToolErrorCategory.CONNECTION] * 2)
    second = RecordingAdapter([ToolErrorCategory.CONNECTION] * 2)
    gateway = _gateway(
        clock,
        (
            (_descriptor(circuit=False), first),
            (_descriptor(ToolId.CALENDAR_COMMIT, circuit=False), second),
        ),
        budgets=RetryBudgetRegistry(maximum_total_attempts=3),
    )
    await gateway.invoke(_context(clock, correlation_id=correlation), Request())
    exhausted = await gateway.invoke(
        _context(
            clock,
            tool_id=ToolId.CALENDAR_COMMIT,
            caller=ToolCaller.CALENDAR_EXECUTOR,
            correlation_id=correlation,
        ),
        Request(),
    )
    assert first.calls == 2 and second.calls == 1
    assert exhausted.result.error_code == "TOOL_RETRY_BUDGET_EXHAUSTED"
    assert (
        gateway.metrics.snapshot()["counts"]["tool_retry_budget_exhausted_total"] == 1
    )


@pytest.mark.asyncio
async def test_new_correlation_receives_independent_retry_budget() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter([ToolErrorCategory.CONNECTION] * 4)
    gateway = _gateway(
        clock,
        ((_descriptor(circuit=False), adapter),),
        budgets=RetryBudgetRegistry(maximum_total_attempts=2),
    )
    first = await gateway.invoke(_context(clock), Request())
    second = await gateway.invoke(_context(clock), Request())
    assert first.result.attempt_count == second.result.attempt_count == 2
    assert adapter.calls == 4


@pytest.mark.asyncio
async def test_attempt_traces_and_invocation_summary_have_distinct_cardinality() -> (
    None
):
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    user_id, correlation = uuid4(), uuid4()
    adapter = RecordingAdapter([ToolErrorCategory.CONNECTION])
    gateway = _gateway(clock, ((_descriptor(), adapter),))
    result = await gateway.invoke(
        _context(clock, user_id=user_id, correlation_id=correlation), Request()
    )
    attempts = gateway.traces.by_correlation_for_user(user_id, correlation)
    summaries = gateway.traces.summaries_by_correlation_for_user(user_id, correlation)
    assert len(attempts) == 2
    assert len(summaries) == 1
    assert summaries[0].attempt_count == 2
    assert summaries[0].status is result.result.status


@pytest.mark.asyncio
async def test_permission_rejection_has_summary_but_no_attempt_trace() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    user_id, correlation = uuid4(), uuid4()
    gateway = _gateway(clock, ((_descriptor(), RecordingAdapter()),))
    await gateway.invoke(
        _context(
            clock,
            user_id=user_id,
            correlation_id=correlation,
            caller=ToolCaller.PROFILE_APPLICATION,
        ),
        Request(),
    )
    assert gateway.traces.by_correlation_for_user(user_id, correlation) == ()
    summaries = gateway.traces.summaries_by_correlation_for_user(user_id, correlation)
    assert len(summaries) == 1 and summaries[0].attempt_count == 0


def test_trace_store_enforces_user_isolation_and_limit() -> None:
    store = ToolTraceStore(limit=3)
    user, other, correlation = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 7, 22, tzinfo=UTC)
    for index in range(5):
        store.add(
            ToolCallTrace(
                invocation_id=uuid4(),
                correlation_id=correlation,
                user_id=user,
                caller="test",
                tool_id=ToolId.CALENDAR_FREE_BUSY,
                tool_version="v1",
                provider_name="stub",
                provider_version="v1",
                attempt_no=index + 1,
                status="SUCCEEDED",
                error_category=None,
                error_code=None,
                latency_ms=1,
                circuit_before=CircuitState.CLOSED,
                circuit_after=CircuitState.CLOSED,
                degradation_mode=ToolDegradationMode.NONE,
                created_at=now,
            )
        )
    assert len(store.by_correlation_for_user(user, correlation, limit=2)) == 2
    assert store.by_correlation_for_user(other, correlation) == ()


@pytest.mark.asyncio
async def test_metrics_separate_invocations_attempts_and_retries() -> None:
    clock = MutableClock(datetime(2026, 7, 22, tzinfo=UTC))
    adapter = RecordingAdapter([ToolErrorCategory.RATE_LIMIT])
    gateway = _gateway(clock, ((_descriptor(), adapter),))
    await gateway.invoke(_context(clock), Request())
    counts = gateway.metrics.snapshot()["counts"]
    assert counts["tool_invocations_total"] == 1
    assert counts["tool_attempts_total"] == 2
    assert counts["tool_retries_total"] == 1
    assert counts["tool_rate_limits_total"] == 1
    assert counts["tool_successes_total"] == 1


def test_metrics_latency_samples_are_bounded_and_low_cardinality() -> None:
    metrics = ToolMetrics(sample_limit=2)
    for latency in (1.0, 2.0, 3.0):
        metrics.record_invocation(
            tool_id=ToolId.CALENDAR_FREE_BUSY,
            status=ToolInvocationStatus.SUCCEEDED,
            error_category=None,
            error_code=None,
            degradation=ToolDegradationMode.NONE,
            circuit_before=CircuitState.CLOSED,
            circuit_after=CircuitState.CLOSED,
            latency_ms=latency,
        )
    snapshot = metrics.snapshot()
    quantiles = snapshot["latency_ms"]["invocation|CALENDAR_FREE_BUSY"]
    assert quantiles == {"p50": 2.0, "p95": 2.0, "p99": 2.0}
    forbidden = ("user_id", "run_id", "operation_key", "http://", "error message")
    assert not any(value in str(snapshot) for value in forbidden)
