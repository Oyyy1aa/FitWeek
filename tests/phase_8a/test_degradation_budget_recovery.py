"""Formal degradation, retry-budget, and worker recovery evidence."""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import build_memory_container
from app.application.calendar_operations import CreateCalendarOperationCommand
from app.application.profile_agent import ParseProfileCommand
from app.calendar_operations.scripted_provider import ScriptedCalendarWriteProvider
from app.config import get_settings
from app.domain.common import utc_now
from app.domain.context.enums import ContextDegradedMode
from app.domain.orchestration.enums import PlanningRunStatus, StepType
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.tools.enums import ToolId
from app.main import create_application
from app.orchestration.calendar_operation_workflow_handlers import (
    ExecuteCalendarOperationItemsHandler,
    FinalizeCalendarOperationHandler,
    VerifyCalendarOperationResultsHandler,
)
from app.orchestration.handler import StepExecutionContext
from app.orchestration.workflow import DeterministicGenerationWorkflow
from app.persistence.memory.memory_repository import InMemoryMemoryRepository
from tests.factories import make_plan
from tests.phase4b_helpers import TEST_WEEK, container, seed_profile
from tests.unit.orchestration.test_phase6b_schedule_recovery import (
    test_schedule_apply_worker_recovery_reuses_the_same_revision as worker_scenario,
)
from tests.unit.planning.test_phase4b_memory_aware import command

pytestmark = pytest.mark.phase_8a


@pytest.mark.asyncio
async def test_no_memory_bridge_preserves_profile_draft_and_plan() -> None:
    value = container()
    await seed_profile(value)
    repository = value.memory_repository
    assert isinstance(repository, InMemoryMemoryRepository)
    repository.set_query_failures(2)
    parsed = await value.profile_agent_service.parse(
        value.development_user,
        ParseProfileCommand(
            client_request_id="phase-8a-no-memory-profile",
            user_message="I want a general fitness profile.",
            current_week=TEST_WEEK,
        ),
    )
    assert parsed.draft.context_degraded_mode is ContextDegradedMode.NO_MEMORY
    first_traces = value.tool_gateway.traces.compatibility_for_user(
        value.development_user.id
    )
    assert len(first_traces) == 2
    assert {item.correlation_id for item in first_traces}.__len__() == 1
    assert first_traces[-1].degradation_mode.value == "NO_MEMORY"

    repository.set_query_failures(2)
    generated = await value.plan_generation_service.generate_plan(
        value.development_user, command("phase-8a-no-memory-plan")
    )
    assert generated.validation.passed
    snapshot = await value.context_application_service.get_snapshot(
        value.development_user,
        generated.metadata.context_snapshot_reference_id,
    )
    assert snapshot.reference.degraded_mode is ContextDegradedMode.NO_MEMORY
    traces = value.tool_gateway.traces.compatibility_for_user(value.development_user.id)
    assert len(traces) == 4
    assert len({item.correlation_id for item in traces}) == 2
    counts = value.tool_gateway.metrics.snapshot()["counts"]
    assert counts["memory_query_bridge_attempts_total"] == 4
    assert counts["memory_query_bridge_retries_total"] == 2
    assert counts["memory_query_no_memory_total"] == 2


@pytest.fixture
def budget_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "false")
    monkeypatch.setenv("TOOL_RETRY_BUDGET_MAXIMUM_ATTEMPTS", "1")
    get_settings.cache_clear()
    with TestClient(create_application()) as value:
        yield value
    get_settings.cache_clear()


def test_formal_session_design_shares_retry_budget_across_tools(
    budget_client: TestClient,
) -> None:
    assert (
        budget_client.put(
            "/api/v1/profiles/me",
            json={
                "experience_level": "BEGINNER",
                "weekly_frequency": 2,
                "max_session_minutes": 60,
                "primary_goal": "GENERAL_FITNESS",
                "scope_confirmed": True,
            },
        ).status_code
        == 200
    )
    container_value = budget_client.app.state.business_container
    duration = container_value.tool_gateway.registry.get(
        ToolId.SESSION_DURATION_CALCULATOR.value, "phase-8a-v1"
    ).adapter
    response = budget_client.post(
        "/api/v1/session-designs",
        json={
            "client_request_id": "phase-8a-shared-budget",
            "target_date": "2026-07-24",
            "target_duration_minutes": 30,
            "location": "HOME",
            "goal": "GENERAL_FITNESS",
        },
    )
    assert response.status_code == 422
    assert duration.invocation_count == 0
    summaries = container_value.tool_gateway.traces.summaries_for_user(
        container_value.development_user.id
    )
    catalog, rejected = summaries[-2:]
    assert catalog.tool_id is ToolId.EXERCISE_CATALOG_SEARCH
    assert rejected.tool_id is ToolId.SESSION_DURATION_CALCULATOR
    assert catalog.correlation_id == rejected.correlation_id
    assert rejected.error_code == "TOOL_RETRY_BUDGET_EXHAUSTED"
    assert rejected.attempt_count == 0
    counts = container_value.tool_gateway.metrics.snapshot()["counts"]
    assert counts["tool_retry_budget_exhausted_total"] == 1


@pytest.mark.asyncio
@pytest.mark.phase_6b
async def test_worker_reaper_reuses_precommitted_schedule_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await worker_scenario(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.phase_6b
async def test_calendar_tool_success_before_step_commit_is_idempotently_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "true")
    monkeypatch.setenv("CALENDAR_WRITE_ENABLED", "true")
    monkeypatch.setenv("CALENDAR_WRITE_PROVIDER", "scripted")
    get_settings.cache_clear()
    value = build_memory_container(get_settings())
    user = value.development_user
    plan = make_plan(user_id=user.id, status=WeeklyPlanStatus.CONFIRMED)
    await value.plan_repository.save(plan)
    draft, created = await value.calendar_operation_service.create_draft(
        user,
        plan.series_id,
        plan.revision,
        CreateCalendarOperationCommand(
            client_request_id="phase-8a-worker-calendar-draft",
            expected_plan_version=plan.version,
            provider="scripted",
            calendar_id="primary",
        ),
    )
    assert created
    draft = await value.calendar_operation_service.approve(
        user, draft.id, draft.version
    )
    created_run = await value.calendar_operation_run_service.create_run(
        user,
        client_request_id="phase-8a-worker-calendar-run",
        draft_id=draft.id,
    )
    run = created_run.run
    worker = value.orchestrator_workers[0]
    assert (await worker.run_once()).outcome == "SUCCEEDED"
    assert (await worker.run_once()).outcome == "SUCCEEDED"

    claimed_at = utc_now() - timedelta(seconds=2)
    claim = await value.orchestration_repository.claim_next_step(
        worker_id="phase-8a-interrupted-calendar-worker",
        lease_duration=timedelta(seconds=1),
        now=claimed_at,
    )
    assert claim is not None
    assert claim.step.step_type is StepType.EXECUTE_CALENDAR_OPERATION_ITEMS
    result = await ExecuteCalendarOperationItemsHandler(
        value.calendar_operation_service, user
    ).execute(StepExecutionContext(claim=claim))
    assert result.output_payload["draft_status"] == "SUCCEEDED"

    adapter = value.tool_gateway.registry.get(
        ToolId.CALENDAR_COMMIT.value, "phase-8a-v1"
    ).adapter
    adapter_calls_after_side_effect = adapter.invocation_count
    bindings_after_side_effect = await value.calendar_operation_service.bindings(
        user,
        provider="scripted",
        calendar_id="primary",
        root_plan_id=plan.series_id,
    )
    operation_keys = tuple(item.operation_key for item in draft.items)
    assert adapter_calls_after_side_effect == len(operation_keys)
    provider = value.calendar_write_gateway._provider  # noqa: SLF001
    assert isinstance(provider, ScriptedCalendarWriteProvider)
    assert len(provider.events) == len(operation_keys)

    reaped = await value.orchestration_repository.reap_expired_steps(
        now=utc_now(), delays_seconds=(0, 0, 0)
    )
    assert [item.id for item in reaped] == [claim.step.id]
    recovery_now = utc_now() + timedelta(seconds=1)
    retry = await value.orchestration_repository.claim_next_step(
        worker_id="phase-8a-recovery-calendar-worker",
        lease_duration=timedelta(seconds=5),
        now=recovery_now,
    )
    assert retry is not None and retry.step.id == claim.step.id
    retry_result = await ExecuteCalendarOperationItemsHandler(
        value.calendar_operation_service, user
    ).execute(StepExecutionContext(claim=retry))
    progression = DeterministicGenerationWorkflow.progression(
        retry.step.step_type, retry_result.outcome
    )
    await value.orchestration_repository.complete_step(
        claim=retry,
        handler_version="phase-6b-execute-calendar-items-v1",
        output_payload=retry_result.output_payload,
        result_reference=retry_result.result_reference,
        next_step_type=progression.next_step_type,
        run_status_after=progression.run_status_after,
        now=recovery_now,
    )
    assert adapter.invocation_count == adapter_calls_after_side_effect
    assert len(provider.events) == len(operation_keys)
    bindings_after_retry = await value.calendar_operation_service.bindings(
        user,
        provider="scripted",
        calendar_id="primary",
        root_plan_id=plan.series_id,
    )
    assert bindings_after_retry == bindings_after_side_effect

    summaries = value.tool_gateway.traces.summaries_by_correlation_for_user(
        user.id, run.id
    )
    assert summaries
    assert {summary.run_id for summary in summaries} == {run.id}
    assert {summary.step_id for summary in summaries} == {claim.step.id}

    verify_now = recovery_now + timedelta(seconds=1)
    verify_claim = await value.orchestration_repository.claim_next_step(
        worker_id="phase-8a-recovery-calendar-worker",
        lease_duration=timedelta(seconds=5),
        now=verify_now,
    )
    assert verify_claim is not None
    verify_result = await VerifyCalendarOperationResultsHandler(
        value.calendar_operation_service, user
    ).execute(StepExecutionContext(claim=verify_claim))
    verify_progression = DeterministicGenerationWorkflow.progression(
        verify_claim.step.step_type, verify_result.outcome
    )
    await value.orchestration_repository.complete_step(
        claim=verify_claim,
        handler_version="phase-6b-verify-calendar-results-v1",
        output_payload=verify_result.output_payload,
        result_reference=verify_result.result_reference,
        next_step_type=verify_progression.next_step_type,
        run_status_after=verify_progression.run_status_after,
        now=verify_now,
    )
    finalize_now = verify_now + timedelta(seconds=1)
    finalize_claim = await value.orchestration_repository.claim_next_step(
        worker_id="phase-8a-recovery-calendar-worker",
        lease_duration=timedelta(seconds=5),
        now=finalize_now,
    )
    assert finalize_claim is not None
    finalize_result = await FinalizeCalendarOperationHandler(
        value.calendar_operation_service, user
    ).execute(StepExecutionContext(claim=finalize_claim))
    finalize_progression = DeterministicGenerationWorkflow.progression(
        finalize_claim.step.step_type, finalize_result.outcome
    )
    await value.orchestration_repository.complete_step(
        claim=finalize_claim,
        handler_version="phase-6b-finalize-calendar-operation-v1",
        output_payload=finalize_result.output_payload,
        result_reference=finalize_result.result_reference,
        next_step_type=finalize_progression.next_step_type,
        run_status_after=finalize_progression.run_status_after,
        now=finalize_now,
    )
    completed = await value.calendar_operation_run_service.get_run(user, run.id)
    assert completed.status is PlanningRunStatus.COMPLETED
    get_settings.cache_clear()
