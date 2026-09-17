"""Application use cases over repository protocols and the memory adapter."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.api.dependencies import build_memory_container
from app.application.errors import BusinessRuleViolation, ConflictError
from app.application.plans import (
    CreatePlanCommand,
    SessionExerciseCommand,
    WorkoutSessionCommand,
)
from app.application.profiles import AddConstraintCommand, UpsertProfileCommand
from app.domain.common import LocationType
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
)
from app.domain.sessions.models import SessionType

pytestmark = pytest.mark.phase_1a

WEEK_START = date(2026, 7, 20)


def profile_command(*, expected_version: int | None = None) -> UpsertProfileCommand:
    return UpsertProfileCommand(
        experience_level=ExperienceLevel.BEGINNER,
        weekly_frequency=2,
        max_session_minutes=45,
        primary_goal=FitnessGoal.GENERAL_FITNESS,
        scope_confirmed=True,
        expected_version=expected_version,
    )


def plan_command(exercise_id: str = "bodyweight_squat") -> CreatePlanCommand:
    sessions: list[WorkoutSessionCommand] = []
    for offset in (0, 2):
        start = datetime(2026, 7, 20 + offset, 10, tzinfo=UTC)
        sessions.append(
            WorkoutSessionCommand(
                scheduled_start=start,
                scheduled_end=start + timedelta(minutes=30),
                location_type=LocationType.HOME,
                session_type=SessionType.MIXED,
                estimated_minutes=30,
                target_difficulty=4,
                exercises=(
                    SessionExerciseCommand(
                        exercise_id=exercise_id,
                        sequence_no=1,
                        sets=2,
                        repetitions=8,
                        duration_seconds=None,
                        rest_seconds=30,
                    ),
                ),
            )
        )
    return CreatePlanCommand(
        week_start=WEEK_START,
        revision=1,
        sessions=tuple(sessions),
    )


@pytest.mark.asyncio
async def test_profile_create_update_and_constraint_lifecycle() -> None:
    container = build_memory_container()
    user = container.development_user

    created = await container.profile_service.upsert_profile(user, profile_command())
    assert created.profile.version == 1

    updated = await container.profile_service.upsert_profile(
        user,
        UpsertProfileCommand(
            experience_level=ExperienceLevel.INTERMEDIATE,
            weekly_frequency=2,
            max_session_minutes=45,
            primary_goal=FitnessGoal.GENERAL_FITNESS,
            scope_confirmed=True,
            expected_version=1,
        ),
    )
    assert updated.profile.version == 2
    assert updated.profile.experience_level is ExperienceLevel.INTERMEDIATE

    constraint = await container.profile_service.add_constraint(
        user,
        AddConstraintCommand(
            constraint_type=ConstraintType.AVAILABLE_EQUIPMENT,
            constraint_value="resistance_band",
            priority=100,
            is_hard=True,
            source=ConstraintSource.USER_EXPLICIT,
        ),
    )
    assert len((await container.profile_service.get_profile(user)).constraints) == 1

    await container.profile_service.delete_constraint(user, constraint.id)
    assert (await container.profile_service.get_profile(user)).constraints == ()


@pytest.mark.asyncio
async def test_profile_stale_update_is_a_conflict() -> None:
    container = build_memory_container()
    await container.profile_service.upsert_profile(
        container.development_user,
        profile_command(),
    )

    with pytest.raises(ConflictError):
        await container.profile_service.upsert_profile(
            container.development_user,
            UpsertProfileCommand(
                experience_level=ExperienceLevel.INTERMEDIATE,
                weekly_frequency=2,
                max_session_minutes=45,
                primary_goal=FitnessGoal.GENERAL_FITNESS,
                scope_confirmed=True,
                expected_version=99,
            ),
        )


@pytest.mark.asyncio
async def test_plan_create_query_session_confirm_and_stale_conflict() -> None:
    container = build_memory_container()
    user = container.development_user
    await container.profile_service.upsert_profile(user, profile_command())

    result = await container.plan_service.create_plan(user, plan_command())
    assert result.validation.passed is True
    assert result.plan.status is WeeklyPlanStatus.VALIDATED
    assert len(await container.plan_service.list_plans(user)) == 1
    assert (
        await container.plan_service.get_plan(user, result.plan.id)
    ).id == result.plan.id

    sessions = await container.session_service.list_plan_sessions(user, result.plan.id)
    assert len(sessions) == 2
    assert (
        await container.session_service.get_session(user, sessions[0].id)
    ).id == sessions[0].id

    confirmed = await container.plan_service.confirm_plan(
        user,
        result.plan.id,
        expected_version=1,
    )
    assert confirmed.status is WeeklyPlanStatus.CONFIRMED
    assert confirmed.version == 2

    with pytest.raises(ConflictError):
        await container.plan_service.confirm_plan(
            user,
            result.plan.id,
            expected_version=1,
        )


@pytest.mark.asyncio
async def test_invalid_plan_is_not_saved() -> None:
    container = build_memory_container()
    user = container.development_user
    await container.profile_service.upsert_profile(user, profile_command())

    with pytest.raises(BusinessRuleViolation) as captured:
        await container.plan_service.create_plan(user, plan_command("unknown_exercise"))

    assert captured.value.code == "PLAN_SAFETY_VALIDATION_FAILED"
    assert {item.code for item in captured.value.violations} == {"EXERCISE_NOT_FOUND"}
    assert await container.plan_service.list_plans(user) == []
