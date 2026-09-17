"""The formal deterministic workflow reaches user wait and final completion."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.api.dependencies import build_memory_container
from app.application.profiles import UpsertProfileCommand
from app.config import Settings
from app.domain.common import LocationType
from app.domain.orchestration.enums import (
    AgentStepStatus,
    PlanningRunStatus,
    StepType,
)
from app.domain.planning.models import AvailabilitySlot, GenerateWeeklyPlanCommand
from app.domain.profiles.models import ExperienceLevel, FitnessGoal
from app.orchestration.retry_policy import RetryPolicy

pytestmark = pytest.mark.phase_2a


def command() -> GenerateWeeklyPlanCommand:
    return GenerateWeeklyPlanCommand(
        week_start=date(2030, 1, 7),
        availability_slots=(
            AvailabilitySlot(
                start=datetime(2030, 1, 7, 9, tzinfo=UTC),
                end=datetime(2030, 1, 7, 9, 30, tzinfo=UTC),
                location_type=LocationType.HOME,
            ),
            AvailabilitySlot(
                start=datetime(2030, 1, 9, 9, tzinfo=UTC),
                end=datetime(2030, 1, 9, 9, 30, tzinfo=UTC),
                location_type=LocationType.HOME,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_full_workflow_waits_for_explicit_confirmation_then_completes() -> None:
    container = build_memory_container(
        Settings(
            orchestrator_enabled=True,
            orchestrator_worker_count=1,
            orchestrator_poll_interval_seconds=0.01,
        )
    )
    user = container.development_user
    await container.profile_service.upsert_profile(
        user,
        UpsertProfileCommand(
            experience_level=ExperienceLevel.BEGINNER,
            weekly_frequency=2,
            max_session_minutes=30,
            primary_goal=FitnessGoal.GENERAL_FITNESS,
            scope_confirmed=True,
        ),
    )
    creation = await container.orchestration_service.create_run(
        user,
        client_request_id="workflow-1",
        command=command(),
    )
    assert creation.run.status is PlanningRunStatus.QUEUED

    worker = container.orchestrator_workers[0]
    for _ in range(4):
        assert (await worker.run_once()).claimed
    waiting = await container.orchestration_service.get_run(user, creation.run.id)
    assert waiting.status is PlanningRunStatus.WAITING_CONFIRMATION
    assert len(await container.plan_service.list_plans(user)) == 1
    steps = await container.orchestration_service.list_steps(user, waiting.id)
    assert [item.step_type for item in steps] == [
        StepType.LOAD_PROFILE_CONTEXT,
        StepType.GENERATE_DETERMINISTIC_PLAN,
        StepType.VERIFY_PLAN_SAFETY,
        StepType.WAIT_FOR_USER_CONFIRMATION,
    ]
    assert steps[-1].status is AgentStepStatus.WAITING_USER
    assert (
        len(await container.orchestration_service.list_checkpoints(user, waiting.id))
        == 3
    )

    resumed = await container.orchestration_service.confirm_and_resume_run(
        user,
        waiting.id,
        expected_plan_version=1,
    )
    assert resumed.status is PlanningRunStatus.WAITING_CONFIRMATION
    assert (await worker.run_once()).outcome == "SUCCEEDED"
    completed = await container.orchestration_service.get_run(user, waiting.id)
    assert completed.status is PlanningRunStatus.COMPLETED
    assert completed.result_reference == waiting.result_reference
    checkpoints = await container.orchestration_service.list_checkpoints(
        user, waiting.id
    )
    assert len(checkpoints) == 5
    assert (await worker.run_once()).outcome == "EMPTY"
    assert len(await container.plan_service.list_plans(user)) == 1
    audit = await container.orchestration_service.list_audit(user, waiting.id)
    assert [event.sequence_no for event in audit] == list(range(1, len(audit) + 1))


@pytest.mark.asyncio
async def test_confirmation_compensation_boundary_is_idempotent() -> None:
    container = build_memory_container(Settings(orchestrator_enabled=True))
    user = container.development_user
    await container.profile_service.upsert_profile(
        user,
        UpsertProfileCommand(
            experience_level=ExperienceLevel.BEGINNER,
            weekly_frequency=2,
            max_session_minutes=30,
            primary_goal=FitnessGoal.GENERAL_FITNESS,
            scope_confirmed=True,
        ),
    )
    run = (
        await container.orchestration_service.create_run(
            user,
            client_request_id="confirm-idempotent",
            command=command(),
        )
    ).run
    for _ in range(4):
        await container.orchestrator_workers[0].run_once()
    first = await container.orchestration_service.confirm_and_resume_run(
        user,
        run.id,
        expected_plan_version=1,
    )
    second = await container.orchestration_service.confirm_and_resume_run(
        user,
        run.id,
        expected_plan_version=1,
    )
    assert first.id == second.id
    steps = await container.orchestration_service.list_steps(user, run.id)
    assert sum(item.step_type is StepType.FINALIZE_RUN for item in steps) == 1
    assert (
        len(await container.orchestration_service.list_checkpoints(user, run.id)) == 4
    )


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_metrics_are_process_local() -> None:
    container = build_memory_container(Settings(orchestrator_enabled=True))
    user = container.development_user
    run = (
        await container.orchestration_service.create_run(
            user,
            client_request_id="cancel-1",
            command=command(),
        )
    ).run
    first = await container.orchestration_service.cancel_run(user, run.id)
    second = await container.orchestration_service.cancel_run(user, run.id)
    assert first.status is second.status is PlanningRunStatus.CANCELLED
    assert container.orchestration_service.metrics().runs_created == 1


def test_retry_policy_does_not_depend_on_real_sleep() -> None:
    policy = RetryPolicy()
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert policy.retry_at(attempt_no=1, now=now) == now
    assert policy.retry_at(attempt_no=2, now=now) == now + timedelta(seconds=1)
    assert policy.retry_at(attempt_no=3, now=now) is None
