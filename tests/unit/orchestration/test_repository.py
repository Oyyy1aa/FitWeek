"""Atomic claim, lease ownership, recovery, checkpoint, and audit tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.domain.common import DomainValidationError
from app.domain.orchestration.enums import AgentStepStatus, PlanningRunStatus, StepType
from app.domain.orchestration.errors import (
    CheckpointConflict,
    RunIdempotencyConflict,
    StepLeaseLost,
)
from app.persistence.memory.orchestration_repository import (
    InMemoryOrchestrationRepository,
)
from app.persistence.mysql.orchestration_repository import (
    MySQLOrchestrationRepository,
)
from tests.unit.orchestration.factories import NOW, make_run, make_step

pytestmark = pytest.mark.phase_2a


def test_mysql_repository_is_constructible_with_session_factory() -> None:
    repository = MySQLOrchestrationRepository(session_factory=None)  # type: ignore[arg-type]

    assert repository.session_factory is None


@pytest.mark.asyncio
async def test_create_is_idempotent_and_conflicting_payload_is_rejected() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    step = make_step(run.id)
    first = await repository.create_run_with_initial_steps(run, (step,))
    second = await repository.create_run_with_initial_steps(run, (step,))
    assert first.created is True
    assert second.created is False
    with pytest.raises(RunIdempotencyConflict):
        await repository.create_run_with_initial_steps(
            make_run(
                run_id=uuid4(),
                client_request_id=run.client_request_id,
                fingerprint="a" * 64,
            ),
            (make_step(uuid4()),),
        )


@pytest.mark.asyncio
async def test_dependency_and_future_schedule_block_claim() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    dependency = make_step(run.id, status=AgentStepStatus.PENDING)
    dependent = make_step(
        run.id,
        sequence_no=2,
        dependencies=(dependency.id,),
    )
    future = replace(
        make_step(run.id, sequence_no=3),
        next_execute_at=NOW + timedelta(hours=1),
    )
    await repository.create_run_with_initial_steps(run, (dependency, dependent, future))
    assert (
        await repository.claim_next_step(
            worker_id="worker-a",
            lease_duration=timedelta(seconds=10),
            now=NOW,
        )
        is None
    )


@pytest.mark.asyncio
async def test_claim_sorting_and_deep_copy_are_stable() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    low = make_step(run.id, priority=1, sequence_no=1)
    high_later = make_step(run.id, priority=10, sequence_no=2)
    high_earlier = make_step(run.id, priority=10, sequence_no=1)
    await repository.create_run_with_initial_steps(run, (low, high_later, high_earlier))
    claim = await repository.claim_next_step(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=10),
        now=NOW,
    )
    assert claim is not None
    assert claim.step.id == high_earlier.id
    claim.step.input_payload["outside"] = True
    stored = {item.id: item for item in await repository.list_steps(run.id)}
    assert "outside" not in stored[high_earlier.id].input_payload


@pytest.mark.asyncio
async def test_four_workers_can_claim_one_step_only_once() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    claims = await asyncio.gather(
        *(
            repository.claim_next_step(
                worker_id=f"worker-{index}",
                lease_duration=timedelta(seconds=10),
                now=NOW,
            )
            for index in range(4)
        )
    )
    assert sum(item is not None for item in claims) == 1


@pytest.mark.asyncio
async def test_four_workers_complete_one_hundred_unique_steps() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    steps = tuple(make_step(run.id, sequence_no=index + 1) for index in range(100))
    await repository.create_run_with_initial_steps(run, steps)
    claimed_ids: list[object] = []

    async def worker(worker_id: str) -> None:
        while True:
            claim = await repository.claim_next_step(
                worker_id=worker_id,
                lease_duration=timedelta(seconds=10),
                now=NOW,
            )
            if claim is None:
                return
            claimed_ids.append(claim.step.id)
            await repository.complete_step(
                claim=claim,
                handler_version="test-v1",
                output_payload={"ok": True},
                result_reference=None,
                next_step_type=None,
                run_status_after=PlanningRunStatus.COLLECTING_PROFILE,
                now=NOW,
            )

    await asyncio.gather(*(worker(f"worker-{index}") for index in range(4)))
    assert len(claimed_ids) == 100
    assert len(set(claimed_ids)) == 100
    assert all(
        item.status is AgentStepStatus.SUCCEEDED
        for item in await repository.list_steps(run.id)
    )


@pytest.mark.asyncio
async def test_heartbeat_renews_lease_and_wrong_owners_are_rejected() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    claim = await repository.claim_next_step(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=1),
        now=NOW,
    )
    assert claim is not None
    with pytest.raises(StepLeaseLost):
        await repository.heartbeat(
            step_id=claim.step.id,
            worker_id="worker-b",
            lease_token=claim.lease_token,
            fencing_token=claim.fencing_token,
            lease_duration=timedelta(seconds=1),
            now=NOW,
        )
    with pytest.raises(StepLeaseLost):
        await repository.heartbeat(
            step_id=claim.step.id,
            worker_id="worker-a",
            lease_token=uuid4(),
            fencing_token=claim.fencing_token,
            lease_duration=timedelta(seconds=1),
            now=NOW,
        )
    with pytest.raises(StepLeaseLost):
        await repository.heartbeat(
            step_id=claim.step.id,
            worker_id="worker-a",
            lease_token=claim.lease_token,
            fencing_token=claim.fencing_token + 1,
            lease_duration=timedelta(seconds=1),
            now=NOW,
        )
    renewed = await repository.heartbeat(
        step_id=claim.step.id,
        worker_id="worker-a",
        lease_token=claim.lease_token,
        fencing_token=claim.fencing_token,
        lease_duration=timedelta(seconds=1),
        now=NOW + timedelta(milliseconds=500),
    )
    assert renewed.lease_expires_at == NOW + timedelta(milliseconds=1500)


@pytest.mark.asyncio
async def test_reaper_reclaims_expired_lease_and_rejects_late_worker() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    old_claim = await repository.claim_next_step(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=1),
        now=NOW,
    )
    assert old_claim is not None
    assert not await repository.reap_expired_steps(
        now=NOW + timedelta(milliseconds=900),
        delays_seconds=(0, 1, 2),
    )
    reaped = await repository.reap_expired_steps(
        now=NOW + timedelta(seconds=2),
        delays_seconds=(0, 1, 2),
    )
    assert [item.id for item in reaped] == [old_claim.step.id]
    assert not await repository.reap_expired_steps(
        now=NOW + timedelta(seconds=2),
        delays_seconds=(0, 1, 2),
    )
    new_claim = await repository.claim_next_step(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=2),
        now=NOW + timedelta(seconds=2),
    )
    assert new_claim is not None
    with pytest.raises(StepLeaseLost):
        await repository.complete_step(
            claim=old_claim,
            handler_version="old-v1",
            output_payload={},
            result_reference=None,
            next_step_type=None,
            run_status_after=PlanningRunStatus.COLLECTING_PROFILE,
            now=NOW + timedelta(seconds=2),
        )
    completed = await repository.complete_step(
        claim=new_claim,
        handler_version="new-v1",
        output_payload={"winner": "worker-b"},
        result_reference=None,
        next_step_type=None,
        run_status_after=PlanningRunStatus.COLLECTING_PROFILE,
        now=NOW + timedelta(seconds=2),
    )
    assert completed.status is AgentStepStatus.SUCCEEDED
    checkpoints = await repository.list_checkpoints(run.id)
    assert len(checkpoints) == 1
    assert checkpoints[0].handler_version == "new-v1"


@pytest.mark.asyncio
async def test_reclaimed_step_uses_a_strictly_higher_fencing_token() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    first = await repository.claim_next_step(
        worker_id="worker-a", lease_duration=timedelta(seconds=1), now=NOW
    )
    assert first is not None
    await repository.reap_expired_steps(
        now=NOW + timedelta(seconds=2), delays_seconds=(0,)
    )
    second = await repository.claim_next_step(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=1),
        now=NOW + timedelta(seconds=2),
    )

    assert second is not None
    assert second.step.fencing_token > first.step.fencing_token


@pytest.mark.asyncio
async def test_max_attempt_lease_expiry_is_permanent() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    await repository.create_run_with_initial_steps(
        run,
        (make_step(run.id, max_attempts=1),),
    )
    await repository.claim_next_step(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=1),
        now=NOW,
    )
    reaped = await repository.reap_expired_steps(
        now=NOW + timedelta(seconds=2),
        delays_seconds=(0, 1, 2),
    )
    assert reaped[0].status is AgentStepStatus.FAILED_PERMANENT
    assert (
        await repository.get_run(run.id)
    ).status is PlanningRunStatus.FAILED_PERMANENT  # type: ignore[union-attr]


def test_payload_rejects_secrets_and_non_json_values() -> None:
    run = make_run()
    with pytest.raises(DomainValidationError):
        replace(run, input_payload={"database_url": "not-allowed"})
    with pytest.raises(DomainValidationError):
        replace(run, input_payload={"value": object()})


@pytest.mark.asyncio
async def test_checkpoint_and_audit_are_ordered_detached_and_secret_free() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    claim = await repository.claim_next_step(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=10),
        now=NOW,
    )
    assert claim is not None
    await repository.complete_step(
        claim=claim,
        handler_version="test-v1",
        output_payload={"safe": "value"},
        result_reference=None,
        next_step_type=StepType.GENERATE_DETERMINISTIC_PLAN,
        run_status_after=PlanningRunStatus.GENERATING_SESSIONS,
        now=NOW,
    )
    checkpoints = await repository.list_checkpoints(run.id)
    checkpoints[0].output_payload["outside"] = True
    assert (
        "outside" not in (await repository.list_checkpoints(run.id))[0].output_payload
    )
    with pytest.raises(CheckpointConflict):
        await repository.save_checkpoint(checkpoints[0])
    audit = await repository.list_audit_events(run.id)
    assert [item.sequence_no for item in audit] == list(range(1, len(audit) + 1))
    rendered = str(audit) + str(checkpoints)
    assert "database_url" not in rendered.casefold()
    assert "traceback" not in rendered.casefold()


@pytest.mark.asyncio
async def test_failure_error_is_bounded_and_connection_details_are_redacted() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    claim = await repository.claim_next_step(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=10),
        now=NOW,
    )
    assert claim is not None
    failed = await repository.fail_step(
        claim=claim,
        error_code="TEST_FAILURE",
        error_message="mysql+asyncmy://user:password@private-host/db",
        retry_at=None,
        now=NOW,
    )
    assert failed.last_error_message == (
        "Handler failed; sensitive details were redacted."
    )
    assert (
        "mysql+asyncmy"
        not in str(await repository.list_audit_events(run.id)).casefold()
    )


@pytest.mark.asyncio
async def test_reset_clears_all_state_and_metrics() -> None:
    repository = InMemoryOrchestrationRepository()
    run = make_run()
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    await repository.reset()
    assert await repository.get_run(run.id) is None
    assert repository.metrics.snapshot().runs_created == 0
