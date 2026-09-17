"""Pure deterministic generator, slot, exercise, and policy behavior."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.common import LocationType
from app.domain.exercises.catalog_seed import CATALOG_SEED
from app.domain.exercises.models import ExerciseDifficulty
from app.domain.planning.models import (
    AvailabilitySlot,
    GenerateWeeklyPlanCommand,
    PlanGenerationError,
)
from app.domain.profiles.models import (
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
)
from app.planning.generator import DeterministicPlanGenerator
from app.safety.engine import SafetyEngine
from tests.factories import (
    TEST_WEEK_START,
    make_constraint,
    make_profile,
)

pytestmark = pytest.mark.phase_1a1


def slots(
    count: int,
    *,
    location: LocationType = LocationType.HOME,
    hour: int = 10,
) -> tuple[AvailabilitySlot, ...]:
    return tuple(
        AvailabilitySlot(
            start=datetime(2026, 7, 20 + index, hour, tzinfo=UTC),
            end=datetime(2026, 7, 20 + index, hour + 1, tzinfo=UTC),
            location_type=location,
        )
        for index in range(count)
    )


def command(
    count: int = 5,
    *,
    location: LocationType = LocationType.HOME,
) -> GenerateWeeklyPlanCommand:
    return GenerateWeeklyPlanCommand(
        week_start=TEST_WEEK_START,
        availability_slots=slots(count, location=location),
    )


def generate(
    *,
    profile=None,
    constraints=(),
    request: GenerateWeeklyPlanCommand | None = None,
    catalog=CATALOG_SEED,
):
    selected_profile = profile or make_profile()
    return DeterministicPlanGenerator().generate_candidate(
        user_id=selected_profile.user_id,
        profile=selected_profile,
        constraints=tuple(constraints),
        catalog=tuple(catalog),
        command=request or command(),
    )


def test_same_input_produces_same_identity_schedule_exercises_and_metadata() -> None:
    profile = make_profile()
    request = command()

    first = generate(profile=profile, request=request)
    second = generate(profile=profile, request=request)

    assert first.plan.id == second.plan.id
    assert first.plan.sessions == second.plan.sessions
    assert first.metadata == second.metadata


@pytest.mark.parametrize("weekly_frequency", [2, 5])
def test_weekly_frequency_is_satisfied(weekly_frequency: int) -> None:
    profile = make_profile(weekly_frequency=weekly_frequency)

    result = generate(profile=profile)

    assert len(result.plan.sessions) == weekly_frequency


def test_beginner_never_receives_intermediate_exercise() -> None:
    result = generate(profile=make_profile(experience_level=ExperienceLevel.BEGINNER))
    catalog = {item.id: item for item in CATALOG_SEED}

    assert all(
        catalog[item.exercise_id].difficulty_level is ExerciseDifficulty.BEGINNER
        for session in result.plan.sessions
        for item in session.exercises
    )


def test_home_plan_without_equipment_uses_only_equipment_free_exercises() -> None:
    result = generate(request=command(location=LocationType.HOME))
    catalog = {item.id: item for item in CATALOG_SEED}

    assert all(
        not catalog[item.exercise_id].required_equipment
        for session in result.plan.sessions
        for item in session.exercises
    )


def test_intermediate_gym_strength_plan_uses_declared_dumbbell() -> None:
    profile = make_profile(
        experience_level=ExperienceLevel.INTERMEDIATE,
        primary_goal=FitnessGoal.BASIC_STRENGTH,
    )
    equipment = make_constraint(
        profile.id,
        ConstraintType.AVAILABLE_EQUIPMENT,
        "dumbbell",
    )

    result = generate(
        profile=profile,
        constraints=(equipment,),
        request=command(location=LocationType.GYM),
    )

    assert any(
        item.exercise_id.startswith("dumbbell_")
        for session in result.plan.sessions
        for item in session.exercises
    )


def test_low_impact_goal_filters_jumping_running_and_high_impact() -> None:
    profile = make_profile(primary_goal=FitnessGoal.LOW_IMPACT_CARDIO)
    result = generate(profile=profile)
    catalog = {item.id: item for item in CATALOG_SEED}

    assert all(
        not (
            catalog[item.exercise_id].feature_tags
            & {"jumping", "running", "high_impact"}
        )
        for session in result.plan.sessions
        for item in session.exercises
    )


def test_running_exclusion_filters_easy_jog_for_intermediate_outdoor_user() -> None:
    profile = make_profile(experience_level=ExperienceLevel.INTERMEDIATE)
    excluded = make_constraint(
        profile.id,
        ConstraintType.EXCLUDED_FEATURE,
        "running",
    )

    result = generate(
        profile=profile,
        constraints=(excluded,),
        request=command(location=LocationType.OUTDOOR),
    )

    assert all(
        item.exercise_id != "easy_jog"
        for session in result.plan.sessions
        for item in session.exercises
    )


def test_unavailable_time_is_removed_from_selected_slot() -> None:
    profile = make_profile()
    blocked = make_constraint(
        profile.id,
        ConstraintType.UNAVAILABLE_TIME,
        "2026-07-20T10:00:00+00:00/2026-07-20T10:30:00+00:00",
    )

    result = generate(profile=profile, constraints=(blocked,))

    assert result.plan.sessions[0].scheduled_start == datetime(
        2026, 7, 20, 10, 30, tzinfo=UTC
    )


def test_insufficient_slots_return_structured_failure() -> None:
    profile = make_profile(weekly_frequency=2)

    with pytest.raises(PlanGenerationError) as captured:
        generate(profile=profile, request=command(count=1))

    assert {item.code for item in captured.value.reasons} >= {
        "INSUFFICIENT_AVAILABILITY"
    }


def test_no_eligible_exercises_returns_structured_failure() -> None:
    profile = make_profile()
    only_dumbbell = next(item for item in CATALOG_SEED if item.id == "dumbbell_row")

    with pytest.raises(PlanGenerationError) as captured:
        generate(profile=profile, catalog=(only_dumbbell,))

    assert {item.code for item in captured.value.reasons} == {"NO_ELIGIBLE_EXERCISES"}


def test_session_duration_never_exceeds_profile_limit() -> None:
    profile = make_profile(max_session_minutes=20)
    result = generate(profile=profile)

    assert all(session.estimated_minutes <= 20 for session in result.plan.sessions)


def test_generated_sessions_do_not_overlap() -> None:
    result = generate()
    ordered = sorted(result.plan.sessions, key=lambda item: item.scheduled_start)

    assert all(
        current.scheduled_end <= following.scheduled_start
        for current, following in zip(ordered, ordered[1:], strict=False)
    )


def test_generated_sessions_are_inside_target_week() -> None:
    result = generate()
    week_start = datetime(2026, 7, 20, tzinfo=UTC)
    week_end = week_start + timedelta(days=7)

    assert all(
        week_start <= session.scheduled_start < session.scheduled_end <= week_end
        for session in result.plan.sessions
    )


def test_sequence_numbers_are_contiguous() -> None:
    result = generate()

    assert all(
        [item.sequence_no for item in session.exercises]
        == list(range(1, len(session.exercises) + 1))
        for session in result.plan.sessions
    )


def test_each_session_contains_distinct_exercises() -> None:
    result = generate()

    assert all(
        len({item.exercise_id for item in session.exercises}) == len(session.exercises)
        for session in result.plan.sessions
    )


def test_generation_metadata_is_complete_and_safe() -> None:
    result = generate()

    assert result.metadata.generation_policy_version == "phase-1a1-v1"
    assert len(result.metadata.catalog_version) == 64
    assert len(result.metadata.input_fingerprint) == 64
    assert result.plan.generation_metadata == result.metadata.as_dict()
    assert "dev-user" not in result.metadata.input_fingerprint


def test_generator_does_not_mutate_inputs() -> None:
    profile = make_profile()
    constraints = (
        make_constraint(
            profile.id,
            ConstraintType.EXCLUDED_FEATURE,
            "jumping",
        ),
    )
    request = command()
    before = deepcopy((profile, constraints, request, CATALOG_SEED))

    generate(profile=profile, constraints=constraints, request=request)

    assert (profile, constraints, request, CATALOG_SEED) == before


def test_generated_candidate_passes_final_safety_engine() -> None:
    profile = make_profile()
    result = generate(profile=profile)

    validation = SafetyEngine().validate_plan(
        profile=profile,
        constraints=(),
        plan=result.plan,
        exercise_catalog={item.id: item for item in CATALOG_SEED},
    )

    assert validation.passed is True


def test_sessions_are_spread_across_distinct_days_when_possible() -> None:
    profile = make_profile(weekly_frequency=5)
    result = generate(profile=profile)

    assert len({item.scheduled_start.date() for item in result.plan.sessions}) == 5
