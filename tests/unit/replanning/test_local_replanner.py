"""Pure local session rebuilding for each supported change type."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.exercises.catalog_seed import CATALOG_SEED
from app.domain.profiles.models import ConstraintType
from app.domain.replanning.models import LocalReplanCommand, PlanChangeType
from app.replanning.impact_analyzer import ChangeImpactAnalyzer
from app.replanning.local_replanner import LocalReplanner
from tests.factories import make_constraint, make_plan, make_profile

pytestmark = pytest.mark.phase_1a2

CATALOG = {item.id: item for item in CATALOG_SEED}


def replan(plan, command: LocalReplanCommand, *, constraints=()):
    profile = make_profile(user_id=plan.user_id)
    impact = ChangeImpactAnalyzer().analyze(
        plan=plan,
        check_ins=(),
        change=command,
        exercise_catalog=CATALOG,
    )
    sessions = LocalReplanner().rebuild(
        source=plan,
        profile=profile,
        constraints=constraints,
        catalog=CATALOG,
        change=command,
        impact=impact,
        fingerprint="a" * 64,
    )
    return impact, sessions


def test_equipment_change_replaces_only_equipment_dependent_exercises() -> None:
    plan = make_plan(exercise_ids=("dumbbell_row", "bodyweight_squat"))
    command = LocalReplanCommand(
        client_request_id="equipment",
        expected_plan_version=1,
        change_type=PlanChangeType.EQUIPMENT_CHANGED,
        effective_from=datetime(2026, 7, 20, tzinfo=UTC),
        available_equipment=(),
    )

    _, sessions = replan(plan, command)

    assert all(
        CATALOG[item.exercise_id].required_equipment == frozenset()
        for session in sessions
        for item in session.exercises
    )
    assert all(
        session.exercises[1].exercise_id == "bodyweight_squat" for session in sessions
    )


def test_excluded_feature_change_preserves_other_legal_actions() -> None:
    plan = make_plan(exercise_ids=("bodyweight_squat", "standing_mobility"))
    profile = make_profile(user_id=plan.user_id)
    constraint = make_constraint(
        profile.id, ConstraintType.EXCLUDED_FEATURE, "bodyweight"
    )
    command = LocalReplanCommand(
        client_request_id="feature",
        expected_plan_version=1,
        change_type=PlanChangeType.EXCLUDED_FEATURE_CHANGED,
        effective_from=datetime(2026, 7, 20, tzinfo=UTC),
        excluded_features=("bodyweight",),
    )

    _, sessions = replan(plan, command, constraints=(constraint,))

    assert all(
        session.exercises[1].exercise_id == "standing_mobility" for session in sessions
    )
    assert all(
        "bodyweight" not in CATALOG[session.exercises[0].exercise_id].feature_tags
        for session in sessions
    )


def test_duration_change_only_rebuilds_sessions_above_new_limit() -> None:
    plan = make_plan(estimated_minutes=45)
    shorter = replace(
        plan.sessions[1],
        scheduled_end=plan.sessions[1].scheduled_start + timedelta(minutes=30),
        estimated_minutes=30,
    )
    plan = replace(
        plan,
        sessions=(plan.sessions[0], shorter),
        estimated_total_minutes=75,
    )
    command = LocalReplanCommand(
        client_request_id="duration",
        expected_plan_version=1,
        change_type=PlanChangeType.SESSION_DURATION_CHANGED,
        effective_from=datetime(2026, 7, 20, tzinfo=UTC),
        max_session_minutes=30,
    )

    impact, sessions = replan(plan, command)

    assert impact.affected_session_ids == (plan.sessions[0].id,)
    assert sessions[0].estimated_minutes == 30
    assert sessions[1] == shorter
