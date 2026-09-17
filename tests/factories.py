"""Deterministic domain factories shared by Phase 1A tests."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from app.domain.common import LocationType
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
    FitnessProfile,
    UserConstraint,
)
from app.domain.sessions.models import (
    SessionExercise,
    SessionType,
    WorkoutSession,
    WorkoutSessionStatus,
)
from app.domain.users.models import UserAccount, UserStatus

TEST_WEEK_START = date(2026, 7, 20)
TEST_NOW = datetime(2026, 7, 17, 0, 0, tzinfo=UTC)


def make_user(*, user_id: UUID | None = None) -> UserAccount:
    return UserAccount(
        id=user_id or uuid4(),
        email="dev-user@fitweek.local",
        timezone="UTC",
        status=UserStatus.ACTIVE,
        created_at=TEST_NOW,
        updated_at=TEST_NOW,
        version=1,
    )


def make_profile(
    *,
    user_id: UUID | None = None,
    profile_id: UUID | None = None,
    weekly_frequency: int = 2,
    max_session_minutes: int = 45,
    scope_confirmed: bool = True,
    version: int = 1,
    experience_level: ExperienceLevel = ExperienceLevel.BEGINNER,
    primary_goal: FitnessGoal = FitnessGoal.GENERAL_FITNESS,
) -> FitnessProfile:
    return FitnessProfile(
        id=profile_id or uuid4(),
        user_id=user_id or uuid4(),
        experience_level=experience_level,
        weekly_frequency=weekly_frequency,
        max_session_minutes=max_session_minutes,
        primary_goal=primary_goal,
        scope_confirmed=scope_confirmed,
        created_at=TEST_NOW,
        updated_at=TEST_NOW,
        version=version,
    )


def make_constraint(
    profile_id: UUID,
    constraint_type: ConstraintType,
    value: str,
    *,
    is_hard: bool = True,
    priority: int = 100,
) -> UserConstraint:
    return UserConstraint(
        id=uuid4(),
        profile_id=profile_id,
        constraint_type=constraint_type,
        constraint_value=value,
        priority=priority,
        is_hard=is_hard,
        source=ConstraintSource.USER_EXPLICIT,
        valid_until=None,
        created_at=TEST_NOW,
        version=1,
    )


def make_session(
    *,
    plan_id: UUID,
    day_offset: int,
    exercise_ids: tuple[str, ...] = ("bodyweight_squat",),
    location: LocationType = LocationType.HOME,
    estimated_minutes: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
) -> WorkoutSession:
    scheduled_start = start or datetime.combine(
        TEST_WEEK_START + timedelta(days=day_offset),
        datetime.min.time(),
        tzinfo=UTC,
    ).replace(hour=10)
    scheduled_end = end or scheduled_start + timedelta(minutes=estimated_minutes)
    return WorkoutSession(
        id=uuid4(),
        plan_id=plan_id,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        location_type=location,
        session_type=SessionType.MIXED,
        estimated_minutes=estimated_minutes,
        target_difficulty=4,
        status=WorkoutSessionStatus.PLANNED,
        exercises=tuple(
            SessionExercise(
                exercise_id=exercise_id,
                sequence_no=index,
                sets=2,
                repetitions=8,
                duration_seconds=None,
                rest_seconds=30,
            )
            for index, exercise_id in enumerate(exercise_ids, start=1)
        ),
        version=1,
    )


def make_plan(
    *,
    user_id: UUID | None = None,
    week_start: date = TEST_WEEK_START,
    exercise_ids: tuple[str, ...] = ("bodyweight_squat",),
    session_count: int = 2,
    location: LocationType = LocationType.HOME,
    estimated_minutes: int = 30,
    status: WeeklyPlanStatus = WeeklyPlanStatus.DRAFT,
    version: int = 1,
) -> WeeklyPlan:
    plan_id = uuid4()
    sessions = tuple(
        make_session(
            plan_id=plan_id,
            day_offset=index * 2,
            exercise_ids=exercise_ids,
            location=location,
            estimated_minutes=estimated_minutes,
        )
        for index in range(session_count)
    )
    confirmed_at = TEST_NOW if status is WeeklyPlanStatus.CONFIRMED else None
    return WeeklyPlan(
        id=plan_id,
        user_id=user_id or uuid4(),
        week_start=week_start,
        status=status,
        revision=1,
        goal_snapshot={"primary_goal": "GENERAL_FITNESS"},
        constraint_snapshot=(),
        estimated_total_minutes=sum(item.estimated_minutes for item in sessions),
        sessions=sessions,
        created_at=TEST_NOW,
        updated_at=TEST_NOW,
        confirmed_at=confirmed_at,
        version=version,
    )
