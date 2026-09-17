"""Real MySQL contracts for durable orchestration transitions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError

from app.domain.common import DomainValidationError
from app.domain.context.enums import AgentType, ContextDegradedMode
from app.domain.context.models import ContextBuildAudit
from app.domain.orchestration.enums import (
    AgentStepStatus,
    AuditEventType,
    PlanningRunStatus,
    StepType,
)
from app.domain.orchestration.errors import RunIdempotencyConflict, StepLeaseLost
from app.persistence.database import Database
from app.persistence.mysql.memory_repository import MySQLMemoryRepository
from app.persistence.mysql.models import (
    AgentStepModel,
    AuditEventModel,
    CheckpointModel,
    PlanningRunModel,
    StepDependencyModel,
    UserAccountModel,
)
from app.persistence.mysql.orchestration_repository import (
    MySQLOrchestrationRepository,
)
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.factories import make_user
from tests.unit.orchestration.factories import NOW, make_run, make_step

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def orchestration_repository(
    mysql_test_database: Database,
) -> tuple[MySQLOrchestrationRepository, object]:
    user = replace(
        make_user(),
        email=f"orchestration-{uuid4().hex}@fitweek.test",
    )
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
    repository = MySQLOrchestrationRepository(mysql_test_database.session_factory)
    try:
        yield repository, user
    finally:
        async with mysql_test_database.session_factory() as session:
            async with session.begin():
                run_ids = (
                    await session.scalars(
                        select(PlanningRunModel.id).where(
                            PlanningRunModel.user_id == str(user.id)
                        )
                    )
                ).all()
                step_ids = (
                    await session.scalars(
                        select(AgentStepModel.id).where(
                            AgentStepModel.run_id.in_(run_ids)
                        )
                    )
                ).all()
                await session.execute(
                    delete(AuditEventModel).where(
                        AuditEventModel.user_id == str(user.id)
                    )
                )
                await session.execute(
                    delete(CheckpointModel).where(CheckpointModel.run_id.in_(run_ids))
                )
                await session.execute(
                    delete(StepDependencyModel).where(
                        StepDependencyModel.step_id.in_(step_ids)
                    )
                )
                await session.execute(
                    delete(AgentStepModel).where(AgentStepModel.run_id.in_(run_ids))
                )
                await session.execute(
                    delete(PlanningRunModel).where(PlanningRunModel.id.in_(run_ids))
                )
                await session.execute(
                    delete(UserAccountModel).where(UserAccountModel.id == str(user.id))
                )


def _run_for(user_id: object):
    return make_run(
        user_id=user_id,  # type: ignore[arg-type]
        client_request_id=f"orchestration-{uuid4().hex}",
    )


async def _delete_generated_orchestration_user(
    database: Database, user_id: str
) -> None:
    """Delete only one focused-test user's durable orchestration facts."""

    async with database.session_factory() as session:
        async with session.begin():
            run_ids = (
                await session.scalars(
                    select(PlanningRunModel.id).where(
                        PlanningRunModel.user_id == user_id
                    )
                )
            ).all()
            step_ids = (
                await session.scalars(
                    select(AgentStepModel.id).where(AgentStepModel.run_id.in_(run_ids))
                )
            ).all()
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id == user_id)
            )
            await session.execute(
                delete(CheckpointModel).where(CheckpointModel.run_id.in_(run_ids))
            )
            await session.execute(
                delete(StepDependencyModel).where(
                    StepDependencyModel.step_id.in_(step_ids)
                    | StepDependencyModel.dependency_step_id.in_(step_ids)
                )
            )
            await session.execute(
                delete(AgentStepModel).where(AgentStepModel.run_id.in_(run_ids))
            )
            await session.execute(
                delete(PlanningRunModel).where(PlanningRunModel.id.in_(run_ids))
            )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id == user_id)
            )


@pytest.mark.asyncio
async def test_execution_scoped_claim_and_reaper_ignore_foreign_user_steps(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
    mysql_test_database: Database,
) -> None:
    """A scoped executor never locks or mutates another user's eligible Step."""

    _repository, target_user = orchestration_repository
    foreign_user = replace(
        make_user(), email=f"orchestration-foreign-{uuid4().hex}@fitweek.test"
    )
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(
        foreign_user
    )
    try:
        target_run = _run_for(target_user.id)  # type: ignore[union-attr]
        foreign_run = _run_for(foreign_user.id)
        target_step = replace(make_step(target_run.id), priority=1)
        foreign_step = replace(make_step(foreign_run.id), priority=100)
        global_repository = MySQLOrchestrationRepository(
            mysql_test_database.session_factory
        )
        await global_repository.create_run_with_initial_steps(
            target_run, (target_step,)
        )
        await global_repository.create_run_with_initial_steps(
            foreign_run, (foreign_step,)
        )

        target_repository = MySQLOrchestrationRepository(
            mysql_test_database.session_factory,
            execution_user_id=target_user.id,  # type: ignore[union-attr]
        )
        target_claim = await target_repository.claim_next_step(
            worker_id="target-claim",
            lease_duration=timedelta(seconds=1),
            now=NOW,
        )
        assert target_claim is not None
        assert target_claim.run.id == target_run.id
        assert target_claim.step.id == target_step.id
        foreign_before = (await global_repository.list_steps(foreign_run.id))[0]
        foreign_run_before = await global_repository.get_run(foreign_run.id)
        foreign_claim = await global_repository.claim_next_step(
            worker_id="foreign-claim",
            lease_duration=timedelta(seconds=1),
            now=NOW,
        )
        assert foreign_claim is not None
        assert foreign_claim.run.id == foreign_run.id
        foreign_claimed = (await global_repository.list_steps(foreign_run.id))[0]
        foreign_run_claimed = await global_repository.get_run(foreign_run.id)
        foreign_audit_claimed = await global_repository.list_audit_events(
            foreign_run.id
        )

        reaped = await target_repository.reap_expired_steps(
            now=NOW + timedelta(seconds=2), delays_seconds=(1,)
        )
        assert [step.id for step in reaped] == [target_step.id]
        assert (await global_repository.list_steps(foreign_run.id))[
            0
        ] == foreign_claimed
        assert await global_repository.get_run(foreign_run.id) == foreign_run_claimed
        assert (
            await global_repository.list_audit_events(foreign_run.id)
        ) == foreign_audit_claimed
        assert foreign_before.fencing_token == 0
        assert foreign_run_before is not None

        unscoped_reaped = await global_repository.reap_expired_steps(
            now=NOW + timedelta(seconds=2), delays_seconds=(1,)
        )
        assert [step.id for step in unscoped_reaped] == [foreign_step.id]
    finally:
        await _delete_generated_orchestration_user(
            mysql_test_database, str(foreign_user.id)
        )


@pytest.mark.asyncio
async def test_round_trip_run_steps_dependencies_checkpoints_and_audit(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    first = make_step(run.id)
    second = make_step(run.id, sequence_no=2, dependencies=(first.id,))

    created = await repository.create_run_with_initial_steps(run, (first, second))

    assert created.created is True
    assert await repository.get_run_for_user(run.id, user.id) == created.run  # type: ignore[union-attr]
    restored_steps = await repository.list_steps(run.id)
    assert {item.id for item in restored_steps} == {first.id, second.id}
    assert next(
        item for item in restored_steps if item.id == second.id
    ).dependency_step_ids == (first.id,)
    assert [
        item.sequence_no for item in await repository.list_audit_events(run.id)
    ] == [
        1,
        2,
        3,
        4,
    ]


@pytest.mark.asyncio
async def test_list_audit_events_ignores_non_orchestration_rows_for_same_run(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    step = make_step(run.id)
    successor = make_step(run.id, sequence_no=2, dependencies=(step.id,))
    await repository.create_run_with_initial_steps(run, (step, successor))
    context_repository = MySQLMemoryRepository(repository.session_factory)
    context_audits = tuple(
        ContextBuildAudit(
            id=uuid4(),
            user_id=user.id,  # type: ignore[union-attr]
            agent_type=AgentType.PLAN_GENERATION,
            context_contract_version="test-v1",
            request_fingerprint=f"context-{index}",
            included_memory_ids=(),
            excluded_memory_ids=(),
            exclusion_reasons={},
            conflicts=(),
            budget_before=0,
            budget_after=0,
            degraded_mode=ContextDegradedMode.NONE,
            created_at=NOW,
            run_id=run.id,
            step_id=step.id,
        )
        for index in range(2)
    )
    for audit in context_audits:
        assert await context_repository.save_context_audit(audit) == audit

    events = await repository.list_audit_events(run.id)
    repeated_events = await repository.list_audit_events(run.id)

    assert events == repeated_events
    assert all(isinstance(event.event_type, AuditEventType) for event in events)
    assert [event.sequence_no for event in events] == [1, 2, 3, 4]
    assert [event.event_type for event in events] == [
        AuditEventType.RUN_CREATED,
        AuditEventType.RUN_STATUS_CHANGED,
        AuditEventType.STEP_CREATED,
        AuditEventType.STEP_CREATED,
    ]
    async with repository.session_factory() as session:
        stored_context_count = await session.scalar(
            select(func.count())
            .select_from(AuditEventModel)
            .where(
                AuditEventModel.run_id == str(run.id),
                AuditEventModel.event_type == "CONTEXT_BUILD_AUDIT",
                AuditEventModel.sequence_no.is_(None),
            )
        )
    assert stored_context_count == len(context_audits)
    for audit in context_audits:
        assert await context_repository.get_context_audit(user.id, audit.id) == audit  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_create_run_idempotency_reuses_same_payload_and_conflicts_on_changed_payload(  # noqa: E501
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    step = make_step(run.id)

    assert (await repository.create_run_with_initial_steps(run, (step,))).created
    assert not (await repository.create_run_with_initial_steps(run, (step,))).created
    conflict_run = replace(run, id=uuid4(), request_fingerprint="a" * 64)
    with pytest.raises(RunIdempotencyConflict):
        await repository.create_run_with_initial_steps(
            conflict_run,
            (make_step(conflict_run.id),),
        )


@pytest.mark.asyncio
async def test_four_concurrent_claimers_claim_one_step_once(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))

    claims = await asyncio.gather(
        *(
            repository.claim_next_step(
                worker_id=f"claim-{index}",
                lease_duration=timedelta(seconds=10),
                now=NOW,
            )
            for index in range(4)
        )
    )

    assert sum(item is not None for item in claims) == 1


@pytest.mark.asyncio
async def test_two_ready_steps_can_be_claimed_by_distinct_workers(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    runs = (_run_for(user.id), _run_for(user.id))  # type: ignore[union-attr]
    await asyncio.gather(
        *(
            repository.create_run_with_initial_steps(run, (make_step(run.id),))
            for run in runs
        )
    )

    first, second = await asyncio.gather(
        repository.claim_next_step(
            worker_id="one", lease_duration=timedelta(seconds=10), now=NOW
        ),
        repository.claim_next_step(
            worker_id="two", lease_duration=timedelta(seconds=10), now=NOW
        ),
    )
    assert first is not None and second is not None
    assert first.step.id != second.step.id


@pytest.mark.asyncio
async def test_claim_transaction_releases_before_handler_boundary(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))

    claim = await repository.claim_next_step(
        worker_id="worker", lease_duration=timedelta(seconds=10), now=NOW
    )

    assert claim is not None
    assert (await repository.get_run(run.id)).current_step_id == claim.step.id  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_heartbeat_requires_owner_token_fence_and_unexpired_lease(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    claim = await repository.claim_next_step(
        worker_id="worker", lease_duration=timedelta(seconds=1), now=NOW
    )
    assert claim is not None
    before_wrong_fence = (await repository.list_steps(run.id))[0]

    with pytest.raises(StepLeaseLost):
        await repository.heartbeat(
            step_id=claim.step.id,
            worker_id="other",
            lease_token=claim.lease_token,
            fencing_token=claim.fencing_token,
            lease_duration=timedelta(seconds=1),
            now=NOW,
        )
    with pytest.raises(StepLeaseLost):
        await repository.heartbeat(
            step_id=claim.step.id,
            worker_id="worker",
            lease_token=uuid4(),
            fencing_token=claim.fencing_token,
            lease_duration=timedelta(seconds=1),
            now=NOW,
        )
    after_wrong_fence = (await repository.list_steps(run.id))[0]
    assert after_wrong_fence.heartbeat_at == before_wrong_fence.heartbeat_at
    assert after_wrong_fence.lease_expires_at == before_wrong_fence.lease_expires_at
    assert after_wrong_fence.version == before_wrong_fence.version
    with pytest.raises(StepLeaseLost):
        await repository.heartbeat(
            step_id=claim.step.id,
            worker_id="worker",
            lease_token=claim.lease_token,
            fencing_token=claim.fencing_token + 1,
            lease_duration=timedelta(seconds=1),
            now=NOW,
        )
    with pytest.raises(StepLeaseLost):
        await repository.heartbeat(
            step_id=claim.step.id,
            worker_id="worker",
            lease_token=claim.lease_token,
            fencing_token=claim.fencing_token,
            lease_duration=timedelta(seconds=1),
            now=NOW + timedelta(seconds=2),
        )
    renewed = await repository.heartbeat(
        step_id=claim.step.id,
        worker_id="worker",
        lease_token=claim.lease_token,
        fencing_token=claim.fencing_token,
        lease_duration=timedelta(seconds=1),
        now=NOW + timedelta(milliseconds=500),
    )
    assert renewed.fencing_token == claim.fencing_token


@pytest.mark.asyncio
async def test_expired_lease_reap_reclaim_rejects_stale_worker_commit(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    old_claim = await repository.claim_next_step(
        worker_id="old", lease_duration=timedelta(seconds=1), now=NOW
    )
    assert old_claim is not None
    await repository.reap_expired_steps(
        now=NOW + timedelta(seconds=2), delays_seconds=(0,)
    )
    new_claim = await repository.claim_next_step(
        worker_id="new",
        lease_duration=timedelta(seconds=1),
        now=NOW + timedelta(seconds=2),
    )
    assert new_claim is not None
    assert new_claim.fencing_token > old_claim.fencing_token
    with pytest.raises(StepLeaseLost):
        await repository.complete_step(
            claim=old_claim,
            handler_version="old",
            output_payload={},
            result_reference=None,
            next_step_type=None,
            run_status_after=PlanningRunStatus.COLLECTING_PROFILE,
            now=NOW + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_complete_checkpoint_successor_and_audit_are_one_transaction(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    claim = await repository.claim_next_step(
        worker_id="worker", lease_duration=timedelta(seconds=10), now=NOW
    )
    assert claim is not None
    completed = await repository.complete_step(
        claim=claim,
        handler_version="v1",
        output_payload={"safe": True},
        result_reference="result-1",
        next_step_type=StepType.GENERATE_DETERMINISTIC_PLAN,
        run_status_after=PlanningRunStatus.GENERATING_SESSIONS,
        now=NOW,
    )
    assert completed.status is AgentStepStatus.SUCCEEDED
    assert len(await repository.list_checkpoints(run.id)) == 1
    assert len(await repository.list_steps(run.id)) == 2


@pytest.mark.asyncio
async def test_retry_schedule_survives_repository_reconstruction(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    claim = await repository.claim_next_step(
        worker_id="worker", lease_duration=timedelta(seconds=10), now=NOW
    )
    assert claim is not None
    retry_at = NOW + timedelta(minutes=1)
    await repository.fail_step(
        claim=claim,
        error_code="TRANSIENT",
        error_message="safe",
        retry_at=retry_at,
        now=NOW,
    )
    restored = await MySQLOrchestrationRepository(
        repository.session_factory
    ).list_steps(run.id)
    assert restored[0].status is AgentStepStatus.RETRY_SCHEDULED
    assert restored[0].next_execute_at == retry_at


@pytest.mark.asyncio
async def test_waiting_user_resume_survives_reconstruction_and_is_idempotent(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    claim = await repository.claim_next_step(
        worker_id="worker", lease_duration=timedelta(seconds=10), now=NOW
    )
    assert claim is not None
    waiting = await repository.mark_waiting_user(
        claim=claim,
        output_payload={"approval": "needed"},
        run_status_after=PlanningRunStatus.GENERATING_SESSIONS,
        now=NOW,
    )
    restored = MySQLOrchestrationRepository(repository.session_factory)
    resumed = await restored.resume_waiting_step(
        run_id=run.id,
        expected_step_id=waiting.id,
        handler_version="v1",
        next_step_type=StepType.GENERATE_DETERMINISTIC_PLAN,
        now=NOW,
    )
    again = await restored.resume_waiting_step(
        run_id=run.id,
        expected_step_id=waiting.id,
        handler_version="v1",
        next_step_type=StepType.GENERATE_DETERMINISTIC_PLAN,
        now=NOW,
    )
    assert again == resumed
    assert len(await restored.list_checkpoints(run.id)) == 1


@pytest.mark.asyncio
async def test_cancel_run_is_atomic_and_terminal(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))

    cancelled = await repository.cancel_run(run_id=run.id, now=NOW)

    assert cancelled.status is PlanningRunStatus.CANCELLED
    assert (await repository.list_steps(run.id))[0].status is AgentStepStatus.CANCELLED


@pytest.mark.asyncio
async def test_user_isolation_safe_payload_and_error_redaction(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    with pytest.raises(DomainValidationError):
        replace(run, input_payload={"database_url": "not-safe"})
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    assert await repository.get_run_for_user(run.id, uuid4()) is None
    claim = await repository.claim_next_step(
        worker_id="worker", lease_duration=timedelta(seconds=10), now=NOW
    )
    assert claim is not None
    failed = await repository.fail_step(
        claim=claim,
        error_code="HANDLER_FAILURE",
        error_message="connection string scheme://user:password@host/database",
        retry_at=None,
        now=NOW,
    )
    assert failed.last_error_code == "HANDLER_FAILURE"
    assert failed.last_error_message == (
        "Handler failed; sensitive details were redacted."
    )
    persisted = (await repository.list_steps(run.id))[0]
    assert persisted.last_error_message == failed.last_error_message


@pytest.mark.asyncio
async def test_create_transaction_failure_rolls_back_run_steps_and_audit(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    invalid_dependency = replace(make_step(run.id), dependency_step_ids=(uuid4(),))

    with pytest.raises(RunIdempotencyConflict):
        await repository.create_run_with_initial_steps(run, (invalid_dependency,))

    assert await repository.get_run(run.id) is None
    assert await repository.list_steps(run.id) == []
    assert await repository.list_audit_events(run.id) == []


@pytest.mark.asyncio
async def test_checkpoint_failure_rolls_back_success_successor_run_and_audit(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    claim = await repository.claim_next_step(
        worker_id="worker", lease_duration=timedelta(seconds=10), now=NOW
    )
    assert claim is not None

    async def fail_checkpoint(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("checkpoint fault injected")

    monkeypatch.setattr(repository, "_save_checkpoint", fail_checkpoint)
    with pytest.raises(RuntimeError, match="checkpoint fault injected"):
        await repository.complete_step(
            claim=claim,
            handler_version="v1",
            output_payload={"safe": True},
            result_reference=None,
            next_step_type=StepType.GENERATE_DETERMINISTIC_PLAN,
            run_status_after=PlanningRunStatus.GENERATING_SESSIONS,
            now=NOW,
        )

    restored_run = await repository.get_run(run.id)
    restored_steps = await repository.list_steps(run.id)
    assert restored_run is not None
    assert restored_run.status is PlanningRunStatus.COLLECTING_PROFILE
    assert [step.status for step in restored_steps] == [AgentStepStatus.RUNNING]
    assert await repository.list_checkpoints(run.id) == []
    assert all(
        event.event_type.value != "STEP_SUCCEEDED"
        for event in await repository.list_audit_events(run.id)
    )


@pytest.mark.asyncio
async def test_running_claim_survives_repository_reconstruction_before_reap(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    claimed: list[object] = []
    ready_to_cancel = asyncio.Event()
    keep_caller_open = asyncio.Event()

    async def claim_then_wait() -> None:
        claim = await repository.claim_next_step(
            worker_id="cancelled-worker",
            lease_duration=timedelta(seconds=1),
            now=NOW,
        )
        claimed.append(claim)
        ready_to_cancel.set()
        await keep_caller_open.wait()

    caller = asyncio.create_task(claim_then_wait())
    await ready_to_cancel.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert claimed[0] is not None

    restored = MySQLOrchestrationRepository(repository.session_factory)
    assert (await restored.list_steps(run.id))[0].status is AgentStepStatus.RUNNING
    await restored.reap_expired_steps(
        now=NOW + timedelta(seconds=2), delays_seconds=(0,)
    )
    recovered = await restored.claim_next_step(
        worker_id="recovery-worker",
        lease_duration=timedelta(seconds=1),
        now=NOW + timedelta(seconds=2),
    )
    assert recovered is not None
    assert recovered.step.fencing_token == 2


class _InjectedMySQLError(Exception):
    def __init__(self, code: int) -> None:
        self.args = (code, "injected MySQL error")


@pytest.mark.asyncio
async def test_mysql_lock_error_handling_is_bounded_and_atomic(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    original = repository._claim_next_step_once
    attempts = 0

    async def deadlock_once(**kwargs: object):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError("SELECT", {}, _InjectedMySQLError(1213))
        return await original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repository, "_claim_next_step_once", deadlock_once)
    claim = await repository.claim_next_step(
        worker_id="worker", lease_duration=timedelta(seconds=10), now=NOW
    )
    assert claim is not None
    assert attempts == 2

    async def non_retryable(**_kwargs: object):
        raise OperationalError("SELECT", {}, _InjectedMySQLError(1045))

    monkeypatch.setattr(repository, "_claim_next_step_once", non_retryable)
    with pytest.raises(OperationalError):
        await repository.claim_next_step(
            worker_id="other", lease_duration=timedelta(seconds=10), now=NOW
        )
    assert (await repository.list_steps(run.id))[0].status is AgentStepStatus.RUNNING


@pytest.mark.asyncio
async def test_mysql_create_run_returns_durable_timestamp_precision(
    orchestration_repository: tuple[MySQLOrchestrationRepository, object],
) -> None:
    """Created results must use MySQL's durable timestamp precision."""

    repository, user = orchestration_repository
    run = _run_for(user.id)  # type: ignore[union-attr]
    run = replace(
        run,
        created_at=run.created_at.replace(microsecond=123456),
        updated_at=run.updated_at.replace(microsecond=123456),
    )
    created = await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    durable = await repository.get_run(run.id)
    assert created.created is True
    assert durable is not None
    assert created.run == durable
    replay = await repository.create_run_with_initial_steps(run, (make_step(run.id),))
    assert replay.created is False
    assert replay.run == durable
