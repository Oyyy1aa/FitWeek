"""Phase 7A Scope, Candidate, validator, and fallback contract tests."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.behavior.models import BehaviorSummary
from app.domain.context.enums import AgentType, ContextSectionName
from app.domain.context.models import ContextBuildCommand
from app.domain.memory.enums import MemoryType
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.recovery.enums import (
    RecoveryActionType,
    RecoveryRedesignGoal,
    RecoveryRequestType,
    RecoveryScopeStatus,
)
from app.domain.recovery.models import RecoveryActionCandidateSet, RecoveryAgentOutput
from app.memory.service import CreateMemoryCommand
from app.orchestration.clock import FakeClock
from app.recovery.candidate_generator import RecoveryCandidateGenerator
from app.recovery.change_impact import RecoveryChangeImpactAnalyzer
from app.recovery.deterministic_fallback import DeterministicRecoveryFallback
from app.recovery.scope_guard import RecoveryScopeGuard
from app.recovery.spacing_validator import RecoverySpacingValidator
from app.recovery.validator import RecoveryAgentBusinessValidator
from tests.factories import make_plan
from tests.phase4a_helpers import memory_container, seed_profile

pytestmark = pytest.mark.phase_7a

NOW = datetime(2026, 7, 17, tzinfo=UTC)


def _summary(user_id: object) -> BehaviorSummary:
    return BehaviorSummary(
        id=uuid4(),
        user_id=user_id,  # type: ignore[arg-type]
        window_start_utc=datetime(2026, 6, 19, tzinfo=UTC),
        window_end_utc=NOW,
        timezone="UTC",
        scheduled_session_count=0,
        checked_in_session_count=0,
        completed_count=0,
        partially_completed_count=0,
        skipped_count=0,
        missing_checkin_count=0,
        completion_rate=None,
        participation_rate=None,
        rpe_sample_count=0,
        average_reported_rpe=None,
        high_reported_rpe_count=0,
        repeated_time_patterns=(),
        repeated_location_patterns=(),
        repeated_skip_patterns=(),
        evidence_references=(),
        conflict_checkin_ids=(),
        policy_version="behavior-summary-policy-v1",
        fingerprint="b" * 64,
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("训练时胸痛", RecoveryScopeStatus.OUT_OF_SCOPE),
        ("I had chest pain", RecoveryScopeStatus.OUT_OF_SCOPE),
        ("我晕厥了", RecoveryScopeStatus.OUT_OF_SCOPE),
        ("severe dizziness", RecoveryScopeStatus.OUT_OF_SCOPE),
        ("呼吸困难", RecoveryScopeStatus.OUT_OF_SCOPE),
        ("术后康复训练", RecoveryScopeStatus.OUT_OF_SCOPE),
        ("最近训练不舒服", RecoveryScopeStatus.NEEDS_REVIEW),
        ("这周工作忙，周六改期", RecoveryScopeStatus.SUPPORTED),
    ],
)
def test_scope_guard(message: str, expected: RecoveryScopeStatus) -> None:
    assert RecoveryScopeGuard().evaluate(message).status is expected


@pytest.mark.asyncio
async def test_recovery_context_places_behavior_before_confirmed_memory() -> None:
    container = memory_container()
    await seed_profile(container)
    await container.memory_application_service.create_memory(
        container.development_user,
        CreateMemoryCommand(
            client_request_id="recovery-context-memory",
            memory_type=MemoryType.PREFERRED_LOCATION,
            key="preferred_location",
            value="home",
            valid_until=None,
        ),
    )
    context = await container.context_application_service.build(
        container.development_user,
        ContextBuildCommand(
            agent_type=AgentType.RECOVERY_AGENT,
            current_task={"request_type": "REDUCE_FUTURE_LOAD"},
            recent_behavior_summary=("Three evening sessions were completed.",),
        ),
    )
    names = tuple(section.name for section in context.sections)
    assert names.index(ContextSectionName.CURRENT_TASK) < names.index(
        ContextSectionName.HARD_CONSTRAINTS
    )
    assert names.index(ContextSectionName.HARD_CONSTRAINTS) < names.index(
        ContextSectionName.PROFILE_SNAPSHOT
    )
    assert names.index(ContextSectionName.PROFILE_SNAPSHOT) < names.index(
        ContextSectionName.RECENT_BEHAVIOR_SUMMARY
    )
    assert names.index(ContextSectionName.RECENT_BEHAVIOR_SUMMARY) < names.index(
        ContextSectionName.RELEVANT_CONFIRMED_MEMORIES
    )


def test_candidate_generator_only_targets_mutable_and_respects_frequency() -> None:
    user_id = uuid4()
    plan = make_plan(
        user_id=user_id,
        status=WeeklyPlanStatus.CONFIRMED,
        version=2,
        session_count=2,
    )
    impact = RecoveryChangeImpactAnalyzer(FakeClock(NOW)).analyze(
        user_id=user_id,
        plan=plan,
        check_ins=(),
        calendar_bindings=(),
        request_type=RecoveryRequestType.REMOVE_FUTURE_SESSION,
        target_session_ids=None,
    )
    values = RecoveryCandidateGenerator().generate(
        request_type=RecoveryRequestType.REMOVE_FUTURE_SESSION,
        target_session_ids=None,
        plan=plan,
        impact=impact,
        behavior=_summary(user_id),
    )
    assert [item.action_type for item in values] == [
        RecoveryActionType.KEEP_CURRENT_PLAN
    ]
    assert all(
        item.target_session_id not in impact.immutable_session_ids for item in values
    )
    assert all(not hasattr(item, "exercise_id") for item in values)
    assert all(not hasattr(item, "scheduled_start") for item in values)


def test_agent_schema_rejects_extra_plan_patch_and_medical_explanation() -> None:
    with pytest.raises(ValidationError):
        RecoveryAgentOutput.model_validate(
            {
                "selected_action_candidate_ids": [],
                "unresolved_session_ids": [],
                "explanation_summary": "safe",
                "plan_patch": {"anything": True},
            }
        )
    with pytest.raises(ValidationError):
        RecoveryAgentOutput(
            selected_action_candidate_ids=(),
            unresolved_session_ids=(),
            explanation_summary="This is a medical diagnosis.",
        )


def test_fallback_is_deterministic_and_uses_only_frozen_ids() -> None:
    user_id = uuid4()
    plan = make_plan(user_id=user_id, status=WeeklyPlanStatus.CONFIRMED, version=2)
    impact = RecoveryChangeImpactAnalyzer(FakeClock(NOW)).analyze(
        user_id=user_id,
        plan=plan,
        check_ins=(),
        calendar_bindings=(),
        request_type=RecoveryRequestType.RESCHEDULE_REQUEST,
        target_session_ids=None,
    )
    candidates = RecoveryCandidateGenerator().generate(
        request_type=RecoveryRequestType.RESCHEDULE_REQUEST,
        target_session_ids=None,
        plan=plan,
        impact=impact,
        behavior=_summary(user_id),
    )

    # Minimal frozen-set test double is sufficient for the pure fallback.
    class Frozen:
        def __init__(self) -> None:
            self.candidates = candidates

    fallback = DeterministicRecoveryFallback()
    first = fallback.build(
        Frozen(),  # type: ignore[arg-type]
        request_type=RecoveryRequestType.RESCHEDULE_REQUEST,
        impact=impact,
    )
    second = fallback.build(
        Frozen(),  # type: ignore[arg-type]
        request_type=RecoveryRequestType.RESCHEDULE_REQUEST,
        impact=impact,
    )
    assert first == second
    assert set(first.selected_action_candidate_ids) <= {item.id for item in candidates}


def test_reduce_load_exposes_controlled_goals_and_fallback_uses_one_per_session() -> (
    None
):
    user_id = uuid4()
    plan = make_plan(user_id=user_id, status=WeeklyPlanStatus.CONFIRMED, version=2)
    impact = RecoveryChangeImpactAnalyzer(FakeClock(NOW)).analyze(
        user_id=user_id,
        plan=plan,
        check_ins=(),
        calendar_bindings=(),
        request_type=RecoveryRequestType.REDUCE_FUTURE_LOAD,
        target_session_ids=None,
    )
    candidates = RecoveryCandidateGenerator().generate(
        request_type=RecoveryRequestType.REDUCE_FUTURE_LOAD,
        target_session_ids=None,
        plan=plan,
        impact=impact,
        behavior=_summary(user_id),
    )
    goals = {
        item.redesign_goal
        for item in candidates
        if item.action_type is RecoveryActionType.REQUEST_SESSION_REDESIGN
    }
    assert goals == {
        RecoveryRedesignGoal.LOWER_LOAD,
        RecoveryRedesignGoal.SHORTER_DURATION,
    }

    class Frozen:
        def __init__(self) -> None:
            self.candidates = candidates

    output = DeterministicRecoveryFallback().build(
        Frozen(),  # type: ignore[arg-type]
        request_type=RecoveryRequestType.REDUCE_FUTURE_LOAD,
        impact=impact,
    )
    selected = [
        item for item in candidates if item.id in output.selected_action_candidate_ids
    ]
    assert len(selected) == len(impact.mutable_session_ids)
    assert len({item.target_session_id for item in selected}) == len(selected)


def test_validator_rejects_unknown_keep_conflict_and_duplicate_ids() -> None:
    user_id = uuid4()
    plan = make_plan(user_id=user_id, status=WeeklyPlanStatus.CONFIRMED, version=2)
    impact = RecoveryChangeImpactAnalyzer(FakeClock(NOW)).analyze(
        user_id=user_id,
        plan=plan,
        check_ins=(),
        calendar_bindings=(),
        request_type=RecoveryRequestType.RESCHEDULE_REQUEST,
        target_session_ids=None,
    )
    candidates = RecoveryCandidateGenerator().generate(
        request_type=RecoveryRequestType.RESCHEDULE_REQUEST,
        target_session_ids=None,
        plan=plan,
        impact=impact,
        behavior=_summary(user_id),
    )
    candidate_set = RecoveryActionCandidateSet(
        id=uuid4(),
        user_id=user_id,
        root_plan_id=plan.series_id,
        source_revision=plan.revision,
        source_plan_version=plan.version,
        behavior_summary_id=uuid4(),
        behavior_summary_fingerprint="a" * 64,
        context_snapshot_reference_id=uuid4(),
        context_fingerprint="b" * 64,
        change_impact_snapshot_id=impact.id,
        candidates=candidates,
        fingerprint="c" * 64,
        policy_version="recovery-candidate-policy-v1",
        prompt_version="recovery-agent-v1",
        created_at=NOW,
    )
    validator = RecoveryAgentBusinessValidator()
    with pytest.raises(ValueError, match="RECOVERY_ACTION_NOT_IN_CANDIDATE_SET"):
        validator.validate(
            RecoveryAgentOutput(
                selected_action_candidate_ids=(uuid4(),),
                unresolved_session_ids=(),
                explanation_summary="Controlled output.",
            ),
            candidate_set=candidate_set,
            impact=impact,
            plan=plan,
        )
    keep = next(
        item
        for item in candidates
        if item.action_type is RecoveryActionType.KEEP_CURRENT_PLAN
    )
    change = next(
        item
        for item in candidates
        if item.action_type is RecoveryActionType.REQUEST_SESSION_RESCHEDULE
    )
    with pytest.raises(ValueError, match="RECOVERY_CONFLICTING_ACTIONS"):
        validator.validate(
            RecoveryAgentOutput(
                selected_action_candidate_ids=(keep.id, change.id),
                unresolved_session_ids=(),
                explanation_summary="Controlled output.",
            ),
            candidate_set=candidate_set,
            impact=impact,
            plan=plan,
        )
    with pytest.raises(ValidationError):
        RecoveryAgentOutput(
            selected_action_candidate_ids=(keep.id, keep.id),
            unresolved_session_ids=(),
            explanation_summary="Controlled output.",
        )


def test_spacing_validator_reports_frequency_consecutive_and_high_load() -> None:
    plan = make_plan(
        user_id=uuid4(),
        status=WeeklyPlanStatus.CONFIRMED,
        version=2,
        session_count=3,
    )
    start = datetime(2026, 7, 20, 10, tzinfo=UTC)
    sessions = tuple(
        replace(
            item,
            scheduled_start=start + timedelta(days=index),
            scheduled_end=start + timedelta(days=index, minutes=30),
            target_difficulty=8,
        )
        for index, item in enumerate(plan.sessions)
    )
    plan = replace(plan, sessions=sessions)
    result = RecoverySpacingValidator().validate(plan=plan, selected=())
    assert not result.passed
    assert set(result.violation_codes) >= {
        "RECOVERY_CONSECUTIVE_DAYS_VIOLATION",
        "RECOVERY_HIGH_LOAD_SPACING_VIOLATION",
    }
