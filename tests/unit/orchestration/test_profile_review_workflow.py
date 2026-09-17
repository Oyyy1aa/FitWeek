"""Phase 3B worker interruption and idempotent recovery contracts."""

from datetime import timedelta
from uuid import UUID

import pytest

from app.domain.orchestration.enums import (
    AgentStepStatus,
    PlanningRunStatus,
    StepType,
    WorkflowType,
)
from app.domain.orchestration.models import AgentStep, PlanningRun
from app.orchestration.clock import FakeClock
from app.orchestration.handler import StepExecutionContext
from app.orchestration.handler_registry import HandlerRegistry
from app.orchestration.profile_workflow_handlers import (
    ApplyProfileDraftHandler,
    FinalizeProfileRunHandler,
)
from app.orchestration.reaper import StepReaper
from app.orchestration.retry_policy import RetryPolicy
from app.orchestration.worker import OrchestrationWorker
from app.persistence.memory.orchestration_repository import (
    InMemoryOrchestrationRepository,
)
from app.profile_application.decision_codec import decision_to_payload
from tests.phase3b_helpers import NOW, USER_ID, build_harness, make_decision, save_draft

pytestmark = pytest.mark.phase_3b


def _worker(
    repository: InMemoryOrchestrationRepository,
    registry: HandlerRegistry,
    clock: FakeClock,
) -> OrchestrationWorker:
    return OrchestrationWorker(
        worker_id="phase-3b-recovery-worker",
        repository=repository,
        registry=registry,
        clock=clock,
        retry_policy=RetryPolicy(),
        lease_duration=timedelta(seconds=1),
        handler_timeout_seconds=1,
        poll_interval_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_worker_loss_after_apply_does_not_duplicate_writes() -> None:
    harness = build_harness()
    draft = await save_draft(harness)
    decision = make_decision(client_request_id="worker-recovery-apply")
    repository = InMemoryOrchestrationRepository()
    clock = FakeClock(NOW)
    run = PlanningRun(
        id=UUID("33000000-0000-4000-8000-000000000001"),
        user_id=USER_ID,
        workflow_type=WorkflowType.PROFILE_AGENT_REVIEW,
        status=PlanningRunStatus.CREATED,
        request_fingerprint="c" * 64,
        input_payload={},
        result_reference=str(draft.id),
        current_step_id=None,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
        version=1,
        client_request_id="worker-recovery-run",
    )
    parse_step = AgentStep(
        id=UUID("33000000-0000-4000-8000-000000000002"),
        run_id=run.id,
        step_type=StepType.PARSE_PROFILE_REQUEST,
        status=AgentStepStatus.READY,
        sequence_no=1,
        priority=100,
        input_payload={},
        output_payload=None,
        dependency_step_ids=(),
        attempt_count=0,
        max_attempts=3,
        next_execute_at=NOW,
        worker_id=None,
        lease_token=None,
        lease_expires_at=None,
        heartbeat_at=None,
        last_error_code=None,
        last_error_message=None,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
        version=1,
    )
    await repository.create_run_with_initial_steps(run, (parse_step,))
    parse_claim = await repository.claim_next_step(
        worker_id="setup-worker",
        lease_duration=timedelta(seconds=1),
        now=NOW,
    )
    assert parse_claim is not None
    parse_output = {
        "draft_id": str(draft.id),
        "draft_version": draft.version,
        "request_id": str(draft.request_id),
        "scope_status": draft.output.scope_status.value,
    }
    await repository.complete_step(
        claim=parse_claim,
        handler_version="phase-3b-test-parse-v1",
        output_payload=parse_output,
        result_reference=str(draft.id),
        next_step_type=StepType.WAIT_FOR_PROFILE_DRAFT_REVIEW,
        run_status_after=PlanningRunStatus.PARSING_PROFILE_REQUEST,
        now=NOW,
    )
    wait_claim = await repository.claim_next_step(
        worker_id="setup-worker",
        lease_duration=timedelta(seconds=1),
        now=NOW,
    )
    assert wait_claim is not None
    waiting = await repository.mark_waiting_user(
        claim=wait_claim,
        output_payload=parse_output,
        run_status_after=PlanningRunStatus.WAITING_PROFILE_REVIEW,
        now=NOW,
    )
    await repository.resume_waiting_step(
        run_id=run.id,
        expected_step_id=waiting.id,
        handler_version="phase-3b-test-user-decision-v1",
        next_step_type=StepType.APPLY_PROFILE_DRAFT,
        now=NOW,
        resume_payload={
            "draft_id": str(draft.id),
            "decision": decision_to_payload(decision),
        },
    )
    apply_handler = ApplyProfileDraftHandler(
        service=harness.applications,
        user=harness.user,
    )
    claim = await repository.claim_next_step(
        worker_id="crashed-worker",
        lease_duration=timedelta(seconds=1),
        now=NOW,
    )
    assert claim is not None

    first_result = await apply_handler.execute(StepExecutionContext(claim=claim))
    profile_after_first = await harness.profiles.get_by_user_id(USER_ID)
    assert profile_after_first is not None and profile_after_first.version == 1
    constraint_count = len(
        await harness.profiles.list_constraints(profile_after_first.id)
    )
    assert constraint_count == 4
    checkpoints_before_recovery = await repository.list_checkpoints(run.id)
    assert {item.step_type for item in checkpoints_before_recovery} == {
        StepType.PARSE_PROFILE_REQUEST,
        StepType.WAIT_FOR_PROFILE_DRAFT_REVIEW,
    }

    clock.advance(timedelta(seconds=2))
    reaped = await StepReaper(
        repository=repository,
        clock=clock,
        retry_policy=RetryPolicy(),
    ).run_once()
    assert reaped.reaped_step_ids == (claim.step.id,)

    registry = HandlerRegistry()
    registry.register(apply_handler)
    registry.register(
        FinalizeProfileRunHandler(
            reviews=harness.reviews,
            profiles=harness.profiles,
            apply_service=harness.applications,
            user=harness.user,
        )
    )
    worker = _worker(repository, registry, clock)
    assert (await worker.run_once()).outcome == "SUCCEEDED"
    assert (await worker.run_once()).outcome == "SUCCEEDED"

    profile_after_retry = await harness.profiles.get_by_user_id(USER_ID)
    assert profile_after_retry == profile_after_first
    assert (
        len(await harness.profiles.list_constraints(profile_after_retry.id))
        == constraint_count
    )
    final_run = await repository.get_run(run.id)
    assert final_run is not None
    assert final_run.status is PlanningRunStatus.COMPLETED
    assert final_run.result_reference == first_result.result_reference
    steps = await repository.list_steps(run.id)
    assert sum(item.step_type is StepType.FINALIZE_PROFILE_RUN for item in steps) == 1
    apply_checkpoint = next(
        item
        for item in await repository.list_checkpoints(run.id)
        if item.step_type is StepType.APPLY_PROFILE_DRAFT
    )
    assert apply_checkpoint.output_payload["resulting_profile_version"] == 1
    assert apply_checkpoint.output_payload["apply_policy_version"] == (
        "profile-draft-apply-v1"
    )
