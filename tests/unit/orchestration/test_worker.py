"""Worker, registry, timeout, retry, and interruption fault injection."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.domain.orchestration.enums import (
    AgentStepStatus,
    AuditEventType,
    StepOutcome,
    StepType,
)
from app.domain.orchestration.models import AgentStep, JsonObject
from app.orchestration.clock import Clock, FakeClock, SystemClock
from app.orchestration.handler import (
    PermanentStepError,
    RetryableStepError,
    StepExecutionContext,
    StepExecutionResult,
)
from app.orchestration.handler_registry import (
    HandlerAlreadyRegistered,
    HandlerNotRegistered,
    HandlerRegistry,
)
from app.orchestration.reaper import StepReaper
from app.orchestration.retry_policy import RetryPolicy
from app.orchestration.worker import OrchestrationWorker
from app.persistence.memory.orchestration_repository import (
    InMemoryOrchestrationRepository,
)
from tests.unit.orchestration.factories import NOW, make_run, make_step

pytestmark = pytest.mark.phase_2a


class SuccessHandler:
    version = "success-v1"

    def __init__(self, step_type: StepType = StepType.LOAD_PROFILE_CONTEXT) -> None:
        self.step_type = step_type
        self.calls = 0

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        self.calls += 1
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload={"step": context.claim.step.step_type.value},
        )


class FlakyHandler(SuccessHandler):
    version = "flaky-v1"

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        self.calls += 1
        if self.calls == 1:
            raise RetryableStepError("temporary test failure")
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload={"attempt": context.claim.step.attempt_count},
        )


class PermanentFailureHandler(SuccessHandler):
    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        self.calls += 1
        raise PermanentStepError("permanent test failure")


class SlowHandler(SuccessHandler):
    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        self.calls += 1
        await asyncio.sleep(1)
        return await super().execute(context)


class HeartbeatObservedHandler(SuccessHandler):
    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        self.calls += 1
        await asyncio.sleep(0.08)
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload={"heartbeat": "observed"},
        )


class InvalidOutputHandler(SuccessHandler):
    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        self.calls += 1
        payload: JsonObject = {"invalid": object()}  # type: ignore[dict-item]
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=payload,
        )


class BlockingHandler(SuccessHandler):
    def __init__(self, step_type: StepType = StepType.LOAD_PROFILE_CONTEXT) -> None:
        super().__init__(step_type)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload={"released": True},
        )


class RepositoryProbeHandler(SuccessHandler):
    def __init__(self, repository: InMemoryOrchestrationRepository) -> None:
        super().__init__()
        self._repository = repository

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        assert await self._repository.get_run(context.claim.run.id) is not None
        return await super().execute(context)


class HeartbeatFenceProbeRepository(InMemoryOrchestrationRepository):
    def __init__(self) -> None:
        super().__init__()
        self.heartbeat_fencing_tokens: list[int] = []

    async def heartbeat(
        self,
        *,
        step_id: UUID,
        worker_id: str,
        lease_token: UUID,
        fencing_token: int,
        lease_duration: timedelta,
        now: datetime,
    ) -> AgentStep:
        self.heartbeat_fencing_tokens.append(fencing_token)
        return await super().heartbeat(
            step_id=step_id,
            worker_id=worker_id,
            lease_token=lease_token,
            fencing_token=fencing_token,
            lease_duration=lease_duration,
            now=now,
        )


def make_worker(
    repository: InMemoryOrchestrationRepository,
    registry: HandlerRegistry,
    clock: Clock,
    *,
    worker_id: str = "worker-a",
    timeout: float = 0.05,
    lease_seconds: float = 1,
) -> OrchestrationWorker:
    return OrchestrationWorker(
        worker_id=worker_id,
        repository=repository,
        registry=registry,
        clock=clock,
        retry_policy=RetryPolicy(max_attempts=3, delays_seconds=(0, 1, 2)),
        lease_duration=timedelta(seconds=lease_seconds),
        handler_timeout_seconds=timeout,
        poll_interval_seconds=0.01,
    )


async def seeded_repository() -> tuple[InMemoryOrchestrationRepository, UUID]:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    return repository, run.id


@pytest.mark.asyncio
async def test_worker_success_runs_handler_outside_repository_lock() -> None:
    repository, run_id = await seeded_repository()
    registry = HandlerRegistry()
    handler = RepositoryProbeHandler(repository)
    registry.register(handler)
    result = await make_worker(repository, registry, FakeClock(NOW)).run_once()
    assert result.outcome == "SUCCEEDED"
    assert handler.calls == 1
    steps = await repository.list_steps(run_id)
    assert steps[0].status is AgentStepStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_retryable_failure_schedules_then_succeeds_without_sleep() -> None:
    repository, run_id = await seeded_repository()
    registry = HandlerRegistry()
    handler = FlakyHandler()
    registry.register(handler)
    clock = FakeClock(NOW)
    worker = make_worker(repository, registry, clock)
    first = await worker.run_once()
    assert first.outcome == AgentStepStatus.RETRY_SCHEDULED.value
    assert (await repository.list_steps(run_id))[0].next_execute_at == NOW
    second = await worker.run_once()
    assert second.outcome == "SUCCEEDED"
    assert handler.calls == 2
    steps = await repository.list_steps(run_id)
    assert steps[0].attempt_count == 2


@pytest.mark.asyncio
async def test_permanent_failure_does_not_retry() -> None:
    repository, _ = await seeded_repository()
    registry = HandlerRegistry()
    handler = PermanentFailureHandler()
    registry.register(handler)
    worker = make_worker(repository, registry, FakeClock(NOW))
    assert (await worker.run_once()).outcome == AgentStepStatus.FAILED_PERMANENT.value
    assert (await worker.run_once()).outcome == "EMPTY"
    assert handler.calls == 1


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_retryable() -> None:
    repository, run_id = await seeded_repository()
    registry = HandlerRegistry()
    registry.register(SlowHandler())
    result = await make_worker(
        repository,
        registry,
        FakeClock(NOW),
        timeout=0.01,
    ).run_once()
    assert result.outcome == AgentStepStatus.RETRY_SCHEDULED.value
    stored = (await repository.list_steps(run_id))[0]
    assert stored.last_error_code == "STEP_HANDLER_TIMEOUT"


@pytest.mark.asyncio
async def test_worker_heartbeats_during_a_long_bounded_handler() -> None:
    current = datetime.now(UTC)
    repository = HeartbeatFenceProbeRepository()
    run = replace(make_run(), created_at=current, updated_at=current)
    step = replace(
        make_step(run.id),
        next_execute_at=current,
        created_at=current,
        updated_at=current,
    )
    await repository.create_run_with_initial_steps(run, (step,))
    run_id = run.id
    registry = HandlerRegistry()
    registry.register(HeartbeatObservedHandler())
    worker = make_worker(
        repository,
        registry,
        SystemClock(),
        timeout=0.2,
        lease_seconds=0.03,
    )
    assert (await worker.run_once()).outcome == "SUCCEEDED"
    events = await repository.list_audit_events(run_id)
    assert AuditEventType.STEP_HEARTBEAT in {item.event_type for item in events}
    assert repository.heartbeat_fencing_tokens
    assert set(repository.heartbeat_fencing_tokens) == {1}


@pytest.mark.asyncio
async def test_missing_handler_and_invalid_output_fail_permanently() -> None:
    missing_repository, _ = await seeded_repository()
    missing = await make_worker(
        missing_repository,
        HandlerRegistry(),
        FakeClock(NOW),
    ).run_once()
    assert missing.outcome == AgentStepStatus.FAILED_PERMANENT.value

    invalid_repository, _ = await seeded_repository()
    registry = HandlerRegistry()
    registry.register(InvalidOutputHandler())
    invalid = await make_worker(
        invalid_repository,
        registry,
        FakeClock(NOW),
    ).run_once()
    assert invalid.outcome == AgentStepStatus.FAILED_PERMANENT.value


@pytest.mark.asyncio
async def test_cancelled_worker_is_reaped_and_new_worker_continues() -> None:
    repository, run_id = await seeded_repository()
    registry = HandlerRegistry()
    blocking = BlockingHandler()
    registry.register(blocking)
    clock = FakeClock(NOW)
    worker_a = make_worker(repository, registry, clock)
    task = asyncio.create_task(worker_a.run_once())
    await blocking.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await repository.list_steps(run_id))[0].status is AgentStepStatus.RUNNING

    clock.advance(timedelta(seconds=2))
    reaper = StepReaper(
        repository=repository,
        clock=clock,
        retry_policy=RetryPolicy(),
    )
    assert (await reaper.run_once()).reaped_step_ids
    registry_b = HandlerRegistry()
    registry_b.register(SuccessHandler())
    worker_b = make_worker(
        repository,
        registry_b,
        clock,
        worker_id="worker-b",
    )
    assert (await worker_b.run_once()).outcome == "SUCCEEDED"
    assert (await repository.list_steps(run_id))[0].attempt_count == 2


@pytest.mark.asyncio
async def test_checkpointed_predecessor_is_not_reexecuted_after_next_step_kill() -> (
    None
):
    repository, run_id = await seeded_repository()
    clock = FakeClock(NOW)
    load = SuccessHandler(StepType.LOAD_PROFILE_CONTEXT)
    generate = BlockingHandler(StepType.GENERATE_DETERMINISTIC_PLAN)
    registry = HandlerRegistry()
    registry.register(load)
    registry.register(generate)
    worker = make_worker(repository, registry, clock)
    assert (await worker.run_once()).outcome == "SUCCEEDED"
    task = asyncio.create_task(worker.run_once())
    await generate.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    clock.advance(timedelta(seconds=2))
    await StepReaper(
        repository=repository,
        clock=clock,
        retry_policy=RetryPolicy(),
    ).run_once()
    registry_b = HandlerRegistry()
    registry_b.register(SuccessHandler(StepType.GENERATE_DETERMINISTIC_PLAN))
    worker_b = make_worker(repository, registry_b, clock, worker_id="worker-b")
    assert (await worker_b.run_once()).outcome == "SUCCEEDED"
    assert load.calls == 1
    checkpoints = await repository.list_checkpoints(run_id)
    assert {item.step_type for item in checkpoints} == {
        StepType.LOAD_PROFILE_CONTEXT,
        StepType.GENERATE_DETERMINISTIC_PLAN,
    }


@pytest.mark.asyncio
async def test_empty_queue_and_worker_loop_shutdown() -> None:
    repository = InMemoryOrchestrationRepository()
    worker = make_worker(repository, HandlerRegistry(), FakeClock(NOW))
    assert (await worker.run_once()).outcome == "EMPTY"
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_loop(stop))
    stop.set()
    await asyncio.wait_for(task, timeout=1)


def test_registry_rejects_duplicates_and_missing_handlers() -> None:
    registry = HandlerRegistry()
    registry.register(SuccessHandler())
    with pytest.raises(HandlerAlreadyRegistered):
        registry.register(SuccessHandler())
    with pytest.raises(HandlerNotRegistered):
        registry.get(StepType.FINALIZE_RUN)
