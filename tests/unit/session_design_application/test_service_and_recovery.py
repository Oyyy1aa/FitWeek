"""Atomic application, full Safety, and worker recovery contracts."""

from dataclasses import replace
from datetime import timedelta

import pytest

from app.api.dependencies import BusinessContainer, build_memory_container
from app.application.errors import (
    ResourceNotFound,
    SessionDesignDraftExpired,
    SessionDesignPlanApplicationFailed,
    SessionDesignPlanSafetyFailed,
    SessionDesignTargetSessionImmutable,
    SessionDesignTargetTypeMismatch,
)
from app.config import get_settings
from app.domain.common import DomainValidationError, LocationType, utc_now
from app.domain.orchestration.enums import AgentStepStatus, StepType
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.profiles.models import ConstraintType, FitnessGoal, FitnessProfile
from app.domain.session_design.models import SessionDesignDraft, SessionDesignRequest
from app.domain.session_design_application.models import ApplySessionDesignCommand
from app.domain.sessions.models import SessionType
from app.orchestration.handler import StepExecutionContext
from app.orchestration.session_design_workflow_handlers import (
    BuildSessionPlanRevisionHandler,
)
from app.persistence.memory import InMemorySessionDesignApplicationRepository
from tests.factories import (
    TEST_WEEK_START,
    make_constraint,
    make_plan,
    make_profile,
    make_user,
)

pytestmark = pytest.mark.phase_5b


async def _container(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_session_type: SessionType | None = None,
) -> tuple[BusinessContainer, tuple[WeeklyPlan, SessionDesignDraft, FitnessProfile]]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "true")
    get_settings.cache_clear()
    container = build_memory_container(get_settings())
    profile = make_profile(user_id=container.development_user.id)
    await container.profile_repository.save(profile)
    plan = make_plan(
        user_id=container.development_user.id,
        status=WeeklyPlanStatus.VALIDATED,
        version=1,
    )
    if target_session_type is not None:
        plan = replace(
            plan,
            sessions=(
                replace(plan.sessions[0], session_type=target_session_type),
                *plan.sessions[1:],
            ),
        )
    await container.plan_repository.save(plan)
    plan = await container.local_replanning_service.confirm_revision(
        container.development_user,
        plan.id,
        1,
        expected_version=1,
    )
    draft, _ = await container.session_design_service.create(
        container.development_user,
        SessionDesignRequest(
            client_request_id="unit-phase-5b-draft",
            target_date=TEST_WEEK_START,
            target_duration_minutes=30,
            location=LocationType.HOME,
            goal=FitnessGoal.GENERAL_FITNESS,
        ),
    )
    draft = await container.session_design_service.review(
        container.development_user,
        draft.id,
        expected_version=draft.version,
        accept=True,
    )
    return container, (plan, draft, profile)


def _command(
    plan: WeeklyPlan, draft: SessionDesignDraft, request_id: str
) -> ApplySessionDesignCommand:
    return ApplySessionDesignCommand(
        client_request_id=request_id,
        expected_draft_version=draft.version,
        root_plan_id=plan.series_id,
        source_revision=plan.revision,
        expected_plan_version=plan.version,
        target_session_id=plan.sessions[0].id,
    )


async def test_atomic_commit_failure_leaves_no_revision_or_applied_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, values = await _container(monkeypatch)
    plan, draft, _ = values
    repository = container.session_design_application_repository
    assert isinstance(repository, InMemorySessionDesignApplicationRepository)
    repository.fail_next_commit_for_test()
    with pytest.raises(SessionDesignPlanApplicationFailed):
        await container.session_design_plan_application_service.apply(
            container.development_user,
            draft.id,
            _command(plan, draft, "atomic-failure"),
        )
    revisions = await container.plan_repository.list_revisions_for_user(
        plan.series_id, container.development_user.id
    )
    stored_draft = await container.session_design_repository.get_draft(
        container.development_user.id, draft.id
    )
    assert [item.revision for item in revisions] == [1]
    assert stored_draft is not None and stored_draft.status.value == "ACCEPTED"
    assert (
        await repository.get_result_by_draft(container.development_user.id, draft.id)
        is None
    )


async def test_applied_draft_lifecycle_is_terminal_and_auditable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, (_, draft, _) = await _container(monkeypatch)
    applied = draft.mark_applied(
        root_plan_id=draft.id,
        revision=2,
        session_id=draft.id,
        application_result_id=draft.id,
        at=utc_now(),
    )
    assert applied.status.value == "APPLIED"
    assert applied.version == draft.version + 1
    assert applied.applied_revision == 2
    assert applied.applied_at is not None
    with pytest.raises(DomainValidationError):
        applied.mark_applied(
            root_plan_id=draft.id,
            revision=3,
            session_id=draft.id,
            application_result_id=draft.id,
            at=utc_now(),
        )
    with pytest.raises(DomainValidationError):
        applied.reject(utc_now())
    await container.model_gateway.close()


async def test_expired_started_other_user_and_type_mismatch_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, (plan, draft, _) = await _container(monkeypatch)
    expiring = replace(
        draft,
        expires_at=draft.created_at + timedelta(microseconds=1),
        version=draft.version + 1,
    )
    expiring = await container.session_design_repository.update_draft(expiring)
    monkeypatch.setattr(
        "app.application.session_design_application.utc_now",
        lambda: draft.created_at + timedelta(seconds=1),
    )
    with pytest.raises(SessionDesignDraftExpired):
        await container.session_design_plan_application_service.preview(
            container.development_user,
            draft.id,
            _command(plan, expiring, "expired"),
        )

    with pytest.raises(ResourceNotFound):
        await container.session_design_plan_application_service.preview(
            make_user(),
            draft.id,
            _command(plan, expiring, "other-user"),
        )

    typed, (typed_plan, typed_draft, _) = await _container(
        monkeypatch, target_session_type=SessionType.CARDIO
    )
    monkeypatch.setattr(
        "app.application.session_design_application.utc_now",
        lambda: typed_draft.created_at + timedelta(seconds=1),
    )
    with pytest.raises(SessionDesignTargetTypeMismatch):
        await typed.session_design_plan_application_service.preview(
            typed.development_user,
            typed_draft.id,
            _command(typed_plan, typed_draft, "type-mismatch"),
        )


async def test_started_target_is_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    container, (plan, draft, _) = await _container(monkeypatch)
    draft = await container.session_design_repository.update_draft(
        replace(
            draft,
            expires_at=plan.sessions[0].scheduled_start + timedelta(hours=1),
            version=draft.version + 1,
        )
    )
    monkeypatch.setattr(
        "app.application.session_design_application.utc_now",
        lambda: plan.sessions[0].scheduled_start,
    )
    with pytest.raises(SessionDesignTargetSessionImmutable):
        await container.session_design_plan_application_service.preview(
            container.development_user,
            draft.id,
            _command(plan, draft, "started"),
        )


async def test_new_constraint_safety_failure_does_not_save_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, values = await _container(monkeypatch)
    plan, draft, profile = values
    selected = await container.exercise_repository.get(draft.exercises[0].exercise_id)
    assert selected is not None and selected.feature_tags
    await container.profile_repository.add_constraint(
        make_constraint(
            profile.id,
            ConstraintType.EXCLUDED_FEATURE,
            sorted(selected.feature_tags)[0],
        )
    )
    with pytest.raises(SessionDesignPlanSafetyFailed):
        await container.session_design_plan_application_service.apply(
            container.development_user,
            draft.id,
            _command(plan, draft, "safety-failure"),
        )
    revisions = await container.plan_repository.list_revisions_for_user(
        plan.series_id, container.development_user.id
    )
    assert [item.revision for item in revisions] == [1]


async def test_worker_lease_recovery_reuses_same_application_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, values = await _container(monkeypatch)
    plan, draft, _ = values
    created = await container.session_design_application_run_service.create_run(
        container.development_user,
        client_request_id="lease-recovery",
        draft_id=draft.id,
        root_plan_id=plan.series_id,
        source_revision=1,
        expected_plan_version=2,
        target_session_id=plan.sessions[0].id,
    )
    worker = container.orchestrator_workers[0]
    assert (await worker.run_once()).outcome == "SUCCEEDED"
    assert (await worker.run_once()).outcome == "SUCCEEDED"

    claimed_at = utc_now()
    claim = await container.orchestration_repository.claim_next_step(
        worker_id="interrupted-worker",
        lease_duration=timedelta(seconds=1),
        now=claimed_at,
    )
    assert (
        claim is not None
        and claim.step.step_type is StepType.BUILD_SESSION_PLAN_REVISION
    )
    handler = BuildSessionPlanRevisionHandler(
        service=container.session_design_plan_application_service,
        user=container.development_user,
    )
    first = await handler.execute(StepExecutionContext(claim=claim))
    assert first.output_payload["application_created"] is True
    reaped = await container.orchestration_repository.reap_expired_steps(
        now=claimed_at + timedelta(seconds=2),
        delays_seconds=(0, 0, 0),
    )
    assert [item.id for item in reaped] == [claim.step.id]
    retry = await container.orchestration_repository.claim_next_step(
        worker_id="recovery-worker",
        lease_duration=timedelta(seconds=1),
        now=claimed_at + timedelta(seconds=3),
    )
    assert retry is not None and retry.step.id == claim.step.id
    second = await handler.execute(StepExecutionContext(claim=retry))
    assert second.output_payload["application_created"] is False
    assert (
        second.output_payload["application_result_id"]
        == first.output_payload["application_result_id"]
    )
    revisions = await container.plan_repository.list_revisions_for_user(
        plan.series_id, container.development_user.id
    )
    assert [item.revision for item in revisions] == [1, 2]
    steps = await container.orchestration_repository.list_steps(created.run.id)
    build = next(
        item for item in steps if item.step_type is StepType.BUILD_SESSION_PLAN_REVISION
    )
    assert build.status is AgentStepStatus.RUNNING


async def test_confirmed_revision_then_resume_is_idempotently_finalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, values = await _container(monkeypatch)
    plan, draft, _ = values
    created = await container.session_design_application_run_service.create_run(
        container.development_user,
        client_request_id="confirm-recovery",
        draft_id=draft.id,
        root_plan_id=plan.series_id,
        source_revision=1,
        expected_plan_version=2,
        target_session_id=plan.sessions[0].id,
    )
    worker = container.orchestrator_workers[0]
    for _ in range(5):
        await worker.run_once()
    waiting_run = await container.session_design_application_run_service.get_run(
        container.development_user, created.run.id
    )
    assert waiting_run.status.value == "WAITING_CONFIRMATION"
    await container.local_replanning_service.confirm_revision(
        container.development_user,
        plan.series_id,
        2,
        expected_version=1,
    )
    resumed = await container.session_design_application_run_service.confirm_run(
        container.development_user,
        created.run.id,
        expected_revision=2,
        expected_plan_version=1,
    )
    assert resumed.status.value == "WAITING_CONFIRMATION"
    assert (await worker.run_once()).outcome == "SUCCEEDED"
    completed = await container.session_design_application_run_service.get_run(
        container.development_user, created.run.id
    )
    assert completed.status.value == "COMPLETED"
    revisions = await container.plan_repository.list_revisions_for_user(
        plan.series_id, container.development_user.id
    )
    assert [item.revision for item in revisions] == [1, 2]
