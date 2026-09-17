"""Worker lease recovery for the Phase 6B Schedule Apply workflow."""

from datetime import timedelta

import pytest

from app.api.dependencies import build_memory_container
from app.config import get_settings
from app.domain.common import LocationType, utc_now
from app.domain.orchestration.enums import StepType
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.scheduling.models import AvailabilityWindow, CreateScheduleDraftCommand
from app.orchestration.handler import StepExecutionContext
from app.orchestration.schedule_application_workflow_handlers import (
    BuildSchedulePlanRevisionHandler,
)
from tests.factories import make_plan, make_profile

pytestmark = pytest.mark.phase_6b


@pytest.mark.phase_8a
async def test_schedule_apply_worker_recovery_reuses_the_same_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "true")
    get_settings.cache_clear()
    container = build_memory_container(get_settings())
    user = container.development_user
    await container.profile_repository.save(make_profile(user_id=user.id))
    source = make_plan(
        user_id=user.id,
        status=WeeklyPlanStatus.VALIDATED,
        version=1,
    )
    await container.plan_repository.save(source)
    source = await container.local_replanning_service.confirm_revision(
        user,
        source.series_id,
        source.revision,
        expected_version=source.version,
    )
    windows = tuple(
        AvailabilityWindow(
            start=session.scheduled_start + timedelta(hours=2),
            end=session.scheduled_end + timedelta(hours=4),
            location=LocationType.HOME,
        )
        for session in source.sessions
    )
    draft, reused = await container.schedule_draft_service.create(
        user,
        CreateScheduleDraftCommand(
            client_request_id="phase6b-worker-draft",
            root_plan_id=source.series_id,
            source_revision=source.revision,
            expected_plan_version=source.version,
            timezone="UTC",
            availability_windows=windows,
        ),
    )
    assert not reused
    draft = await container.schedule_draft_service.review(
        user,
        draft.id,
        expected_version=draft.version,
        accept=True,
    )
    await container.schedule_application_run_service.create_run(
        user,
        client_request_id="phase6b-worker-recovery",
        draft_id=draft.id,
        expected_draft_version=draft.version,
        root_plan_id=source.series_id,
        source_revision=source.revision,
        expected_plan_version=source.version,
    )
    worker = container.orchestrator_workers[0]
    for _ in range(3):
        assert (await worker.run_once()).outcome == "SUCCEEDED"

    claimed_at = utc_now()
    claim = await container.orchestration_repository.claim_next_step(
        worker_id="phase6b-interrupted-worker",
        lease_duration=timedelta(seconds=1),
        now=claimed_at,
    )
    assert claim is not None
    assert claim.step.step_type is StepType.BUILD_SCHEDULE_PLAN_REVISION
    handler = BuildSchedulePlanRevisionHandler(
        container.schedule_plan_application_service,
        user,
    )
    first = await handler.execute(StepExecutionContext(claim=claim))
    assert first.output_payload["created_revision"] == 2

    reaped = await container.orchestration_repository.reap_expired_steps(
        now=claimed_at + timedelta(seconds=2),
        delays_seconds=(0, 0, 0),
    )
    assert [item.id for item in reaped] == [claim.step.id]
    retry = await container.orchestration_repository.claim_next_step(
        worker_id="phase6b-recovery-worker",
        lease_duration=timedelta(seconds=1),
        now=claimed_at + timedelta(seconds=3),
    )
    assert retry is not None and retry.step.id == claim.step.id
    second = await handler.execute(StepExecutionContext(claim=retry))
    assert (
        second.output_payload["application_result_id"]
        == first.output_payload["application_result_id"]
    )
    assert (
        second.output_payload["created_plan_revision_id"]
        == first.output_payload["created_plan_revision_id"]
    )
    revisions = await container.plan_repository.list_revisions_for_user(
        source.series_id,
        user.id,
    )
    assert [item.revision for item in revisions] == [1, 2]
    get_settings.cache_clear()
