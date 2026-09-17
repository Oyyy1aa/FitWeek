"""Safety Engine rules exercised without repositories or external services."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.domain.common import LocationType
from app.domain.exercises.catalog_seed import CATALOG_SEED
from app.domain.exercises.models import Exercise
from app.domain.plans.models import WeeklyPlan
from app.domain.profiles.models import ConstraintType, FitnessProfile, UserConstraint
from app.domain.sessions.models import SessionExercise, WorkoutSession
from app.safety.engine import SafetyEngine
from app.safety.models import SafetyValidationResult
from tests.factories import (
    TEST_WEEK_START,
    make_constraint,
    make_plan,
    make_profile,
)

pytestmark = pytest.mark.phase_1a


def _catalog() -> dict[str, Exercise]:
    return {exercise.id: exercise for exercise in CATALOG_SEED}


def _codes(result: SafetyValidationResult) -> set[str]:
    return {violation.code for violation in result.violations}


def _validate(
    *,
    profile: FitnessProfile | None = None,
    constraints: tuple[UserConstraint, ...] = (),
    plan: WeeklyPlan | None = None,
    catalog: dict[str, Exercise] | None = None,
) -> SafetyValidationResult:
    current_profile = profile or make_profile()
    current_plan = plan or make_plan(user_id=current_profile.user_id)
    return SafetyEngine().validate_plan(
        profile=current_profile,
        constraints=constraints,
        plan=current_plan,
        exercise_catalog=catalog or _catalog(),
    )


def _replace_session(
    plan: WeeklyPlan,
    index: int,
    session: WorkoutSession,
) -> WeeklyPlan:
    sessions = list(plan.sessions)
    sessions[index] = session
    session_tuple = tuple(sessions)
    return replace(
        plan,
        sessions=session_tuple,
        estimated_total_minutes=sum(item.estimated_minutes for item in session_tuple),
    )


def test_valid_plan_passes() -> None:
    result = _validate()

    assert result.passed is True
    assert result.violations == ()


def test_unknown_exercise_is_rejected() -> None:
    result = _validate(plan=make_plan(exercise_ids=("not_in_catalog",)))

    assert result.passed is False
    assert _codes(result) == {"EXERCISE_NOT_FOUND"}
    assert result.violations[0].exercise_id == "not_in_catalog"


def test_disabled_exercise_is_rejected() -> None:
    result = _validate(plan=make_plan(exercise_ids=("burpee",)))

    assert "EXERCISE_DISABLED" in _codes(result)


def test_unavailable_equipment_is_rejected() -> None:
    result = _validate(plan=make_plan(exercise_ids=("dumbbell_row",)))

    assert "EQUIPMENT_MISMATCH" in _codes(result)
    assert {item.exercise_id for item in result.violations} == {"dumbbell_row"}


def test_exercise_location_mismatch_is_rejected() -> None:
    result = _validate(
        plan=make_plan(
            exercise_ids=("brisk_walk",),
            location=LocationType.HOME,
        )
    )

    assert "LOCATION_MISMATCH" in _codes(result)


def test_hard_jumping_exclusion_is_rejected() -> None:
    profile = make_profile()
    constraint = make_constraint(
        profile.id,
        ConstraintType.EXCLUDED_FEATURE,
        "jumping",
    )

    result = _validate(
        profile=profile,
        constraints=(constraint,),
        plan=make_plan(
            user_id=profile.user_id,
            exercise_ids=("jumping_jack",),
        ),
    )

    assert "EXCLUDED_FEATURE" in _codes(result)


def test_hard_running_exclusion_is_rejected() -> None:
    profile = make_profile()
    constraint = make_constraint(
        profile.id,
        ConstraintType.EXCLUDED_FEATURE,
        "running",
    )

    result = _validate(
        profile=profile,
        constraints=(constraint,),
        plan=make_plan(
            user_id=profile.user_id,
            exercise_ids=("easy_jog",),
            location=LocationType.OUTDOOR,
        ),
    )

    assert "EXCLUDED_FEATURE" in _codes(result)


def test_session_over_profile_maximum_is_rejected() -> None:
    profile = make_profile(max_session_minutes=45)

    result = _validate(
        profile=profile,
        plan=make_plan(
            user_id=profile.user_id,
            estimated_minutes=50,
        ),
    )

    assert "SESSION_TOO_LONG" in _codes(result)


def test_weekly_frequency_mismatch_is_rejected() -> None:
    profile = make_profile(weekly_frequency=2)

    result = _validate(
        profile=profile,
        plan=make_plan(user_id=profile.user_id, session_count=1),
    )

    assert "WEEKLY_FREQUENCY_MISMATCH" in _codes(result)


def test_overlapping_sessions_are_rejected() -> None:
    profile = make_profile()
    plan = make_plan(user_id=profile.user_id)
    first = plan.sessions[0]
    overlapping = replace(
        plan.sessions[1],
        scheduled_start=first.scheduled_start + timedelta(minutes=15),
        scheduled_end=first.scheduled_end + timedelta(minutes=15),
    )
    plan = _replace_session(plan, 1, overlapping)

    result = _validate(profile=profile, plan=plan)

    assert "SESSION_OVERLAP" in _codes(result)
    assert any(item.session_id == overlapping.id for item in result.violations)


def test_session_outside_target_week_is_rejected() -> None:
    profile = make_profile()
    plan = make_plan(user_id=profile.user_id)
    outside_start = datetime.combine(
        TEST_WEEK_START + timedelta(days=7),
        datetime.min.time(),
        tzinfo=UTC,
    ).replace(hour=10)
    outside = replace(
        plan.sessions[0],
        scheduled_start=outside_start,
        scheduled_end=outside_start + timedelta(minutes=30),
    )
    plan = _replace_session(plan, 0, outside)

    result = _validate(profile=profile, plan=plan)

    assert "SESSION_OUTSIDE_WEEK" in _codes(result)


def test_unconfirmed_scope_is_rejected() -> None:
    profile = make_profile(scope_confirmed=False)

    result = _validate(
        profile=profile,
        plan=make_plan(user_id=profile.user_id),
    )

    assert _codes(result) == {"SCOPE_NOT_CONFIRMED"}


def test_multiple_violations_are_aggregated_in_one_result() -> None:
    profile = make_profile(
        weekly_frequency=2,
        max_session_minutes=45,
        scope_confirmed=False,
    )
    plan = make_plan(
        user_id=profile.user_id,
        exercise_ids=("not_in_catalog",),
        session_count=1,
        estimated_minutes=50,
    )

    result = _validate(profile=profile, plan=plan)

    assert result.passed is False
    assert {
        "SCOPE_NOT_CONFIRMED",
        "WEEKLY_FREQUENCY_MISMATCH",
        "SESSION_TOO_LONG",
        "EXERCISE_NOT_FOUND",
    }.issubset(_codes(result))
    assert len(result.violations) >= 4


def test_validation_does_not_mutate_any_input() -> None:
    profile = make_profile()
    constraints = (
        make_constraint(
            profile.id,
            ConstraintType.AVAILABLE_EQUIPMENT,
            "dumbbell",
        ),
    )
    plan = make_plan(
        user_id=profile.user_id,
        exercise_ids=("dumbbell_row",),
    )
    catalog = _catalog()
    before = deepcopy((profile, constraints, plan, catalog))

    result = _validate(
        profile=profile,
        constraints=constraints,
        plan=plan,
        catalog=catalog,
    )

    assert result.passed is True
    assert (profile, constraints, plan, catalog) == before


def test_naive_session_timestamps_are_rejected() -> None:
    profile = make_profile()
    plan = make_plan(user_id=profile.user_id)
    session = plan.sessions[0]
    object.__setattr__(
        session,
        "scheduled_start",
        session.scheduled_start.replace(tzinfo=None),
    )
    object.__setattr__(
        session,
        "scheduled_end",
        session.scheduled_end.replace(tzinfo=None),
    )

    result = _validate(profile=profile, plan=plan)

    assert "NAIVE_DATETIME" in _codes(result)


def test_non_utc_session_timestamps_are_rejected() -> None:
    profile = make_profile()
    plan = make_plan(user_id=profile.user_id)
    session = plan.sessions[0]
    utc_plus_eight = timezone(timedelta(hours=8))
    object.__setattr__(
        session,
        "scheduled_start",
        session.scheduled_start.astimezone(utc_plus_eight),
    )
    object.__setattr__(
        session,
        "scheduled_end",
        session.scheduled_end.astimezone(utc_plus_eight),
    )

    result = _validate(profile=profile, plan=plan)

    assert "INVALID_SESSION_TIME" in _codes(result)


def test_empty_session_is_rejected() -> None:
    profile = make_profile()
    plan = make_plan(user_id=profile.user_id)
    object.__setattr__(plan.sessions[0], "exercises", ())

    result = _validate(profile=profile, plan=plan)

    assert "EMPTY_SESSION" in _codes(result)


def test_duplicate_sequence_number_is_rejected() -> None:
    profile = make_profile()
    plan = make_plan(user_id=profile.user_id)
    session = plan.sessions[0]
    first = session.exercises[0]
    duplicate = replace(first, exercise_id="wall_push_up")
    object.__setattr__(session, "exercises", (first, duplicate))

    result = _validate(profile=profile, plan=plan)

    assert "DUPLICATE_SEQUENCE" in _codes(result)


def test_one_minute_duration_rounding_is_allowed() -> None:
    profile = make_profile()
    plan = make_plan(user_id=profile.user_id)
    session = plan.sessions[0]
    rounded = replace(
        session,
        scheduled_end=session.scheduled_end + timedelta(minutes=1),
    )

    result = _validate(
        profile=profile,
        plan=_replace_session(plan, 0, rounded),
    )

    assert result.passed is True


def test_duration_difference_over_one_minute_is_rejected() -> None:
    profile = make_profile()
    plan = make_plan(user_id=profile.user_id)
    session = plan.sessions[0]
    mismatched = replace(
        session,
        scheduled_end=session.scheduled_end + timedelta(seconds=61),
    )

    result = _validate(
        profile=profile,
        plan=_replace_session(plan, 0, mismatched),
    )

    assert "SESSION_DURATION_MISMATCH" in _codes(result)


def test_structured_max_session_constraint_uses_strictest_limit() -> None:
    profile = make_profile(max_session_minutes=45)
    constraint = make_constraint(
        profile.id,
        ConstraintType.MAX_SESSION_MINUTES,
        "20",
    )

    result = _validate(
        profile=profile,
        constraints=(constraint,),
        plan=make_plan(user_id=profile.user_id, estimated_minutes=30),
    )

    assert "SESSION_TOO_LONG" in _codes(result)


def test_hard_allowed_location_constraint_is_enforced() -> None:
    profile = make_profile()
    constraint = make_constraint(
        profile.id,
        ConstraintType.ALLOWED_LOCATION,
        LocationType.HOME.value,
    )

    result = _validate(
        profile=profile,
        constraints=(constraint,),
        plan=make_plan(
            user_id=profile.user_id,
            location=LocationType.OUTDOOR,
        ),
    )

    assert "LOCATION_MISMATCH" in _codes(result)


def test_hard_unavailable_time_interval_is_enforced() -> None:
    profile = make_profile()
    constraint = make_constraint(
        profile.id,
        ConstraintType.UNAVAILABLE_TIME,
        "2026-07-20T09:45:00+00:00/2026-07-20T10:15:00+00:00",
    )

    result = _validate(
        profile=profile,
        constraints=(constraint,),
        plan=make_plan(user_id=profile.user_id),
    )

    assert "UNAVAILABLE_TIME_CONFLICT" in _codes(result)


def test_end_not_after_start_is_rejected() -> None:
    profile = make_profile()
    plan = make_plan(user_id=profile.user_id)
    session = plan.sessions[0]
    object.__setattr__(session, "scheduled_end", session.scheduled_start)

    result = _validate(profile=profile, plan=plan)

    assert "INVALID_SESSION_TIME" in _codes(result)


def test_soft_feature_exclusion_does_not_reject_plan() -> None:
    profile = make_profile()
    constraint = make_constraint(
        profile.id,
        ConstraintType.EXCLUDED_FEATURE,
        "jumping",
        is_hard=False,
    )

    result = _validate(
        profile=profile,
        constraints=(constraint,),
        plan=make_plan(
            user_id=profile.user_id,
            exercise_ids=("jumping_jack",),
        ),
    )

    assert result.passed is True


def test_declared_equipment_allows_required_exercise() -> None:
    profile = make_profile()
    constraint = make_constraint(
        profile.id,
        ConstraintType.AVAILABLE_EQUIPMENT,
        "DUMBBELL",
    )

    result = _validate(
        profile=profile,
        constraints=(constraint,),
        plan=make_plan(
            user_id=profile.user_id,
            exercise_ids=("dumbbell_row",),
        ),
    )

    assert result.passed is True


def test_expired_hard_constraint_is_not_applied() -> None:
    profile = make_profile()
    constraint = make_constraint(
        profile.id,
        ConstraintType.EXCLUDED_FEATURE,
        "jumping",
    )
    expired = replace(
        constraint,
        valid_until=datetime(2026, 7, 19, 23, 59, tzinfo=UTC),
    )

    result = _validate(
        profile=profile,
        constraints=(expired,),
        plan=make_plan(
            user_id=profile.user_id,
            exercise_ids=("jumping_jack",),
        ),
    )

    assert result.passed is True


def test_each_violation_exposes_only_structured_client_safe_fields() -> None:
    result = _validate(plan=make_plan(exercise_ids=("not_in_catalog",)))

    violation = result.violations[0]
    assert violation.code == "EXERCISE_NOT_FOUND"
    assert violation.message
    assert violation.path == "sessions[0].exercises[0].exercise_id"
    assert violation.session_id is not None
    assert violation.exercise_id == "not_in_catalog"


def test_session_exercise_fixture_is_a_real_domain_value() -> None:
    """Guard the test setup itself from accidentally bypassing domain validation."""

    exercise = SessionExercise(
        exercise_id="bodyweight_squat",
        sequence_no=1,
        sets=2,
        repetitions=8,
        duration_seconds=None,
        rest_seconds=30,
    )

    assert exercise.sequence_no == 1
