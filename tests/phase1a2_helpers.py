"""Deterministic Phase 1A.2 application fixtures without external services."""

from datetime import UTC, datetime
from uuid import uuid4

from app.api.dependencies import BusinessContainer, build_memory_container
from app.application.checkins import CreateCheckInCommand
from app.application.profiles import AddConstraintCommand, UpsertProfileCommand
from app.domain.checkins.models import CheckInStatus, WorkoutCheckIn
from app.domain.common import LocationType
from app.domain.planning.models import AvailabilitySlot, GenerateWeeklyPlanCommand
from app.domain.plans.models import WeeklyPlan
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
)
from app.domain.replanning.models import LocalReplanCommand, PlanChangeType
from app.domain.sessions.models import SessionType
from tests.factories import TEST_WEEK_START


async def prepared_container(
    *, weekly_frequency: int = 2
) -> tuple[BusinessContainer, WeeklyPlan]:
    container = build_memory_container()
    await container.profile_service.upsert_profile(
        container.development_user,
        UpsertProfileCommand(
            experience_level=ExperienceLevel.BEGINNER,
            weekly_frequency=weekly_frequency,
            max_session_minutes=45,
            primary_goal=FitnessGoal.GENERAL_FITNESS,
            scope_confirmed=True,
        ),
    )
    generated = await container.plan_generation_service.generate_plan(
        container.development_user,
        generation_command(weekly_frequency),
    )
    confirmed = await container.plan_service.confirm_plan(
        container.development_user,
        generated.plan.id,
        expected_version=generated.plan.version,
    )
    return container, confirmed


def generation_command(frequency: int = 2) -> GenerateWeeklyPlanCommand:
    return GenerateWeeklyPlanCommand(
        week_start=TEST_WEEK_START,
        availability_slots=tuple(
            AvailabilitySlot(
                start=datetime(2026, 7, 20 + index, 10, tzinfo=UTC),
                end=datetime(2026, 7, 20 + index, 11, tzinfo=UTC),
                location_type=LocationType.HOME,
            )
            for index in range(frequency)
        ),
        preferred_session_types=(SessionType.MIXED,),
    )


def check_in_command(
    *,
    client_event_id: str = "event-1",
    status: CheckInStatus = CheckInStatus.COMPLETED,
    actual_minutes: int | None = 30,
) -> CreateCheckInCommand:
    return CreateCheckInCommand(
        client_event_id=client_event_id,
        status=status,
        actual_minutes=actual_minutes,
        perceived_effort=5,
        note="Completed as planned.",
        occurred_at=datetime(2026, 7, 20, 11, tzinfo=UTC),
    )


def make_check_in(**changes: object) -> WorkoutCheckIn:
    values = {
        "id": uuid4(),
        "client_event_id": "event-1",
        "user_id": uuid4(),
        "plan_id": uuid4(),
        "plan_revision": 1,
        "session_id": uuid4(),
        "status": CheckInStatus.COMPLETED,
        "actual_minutes": 30,
        "perceived_effort": 5,
        "note": "Done.",
        "occurred_at": datetime(2026, 7, 20, 11, tzinfo=UTC),
        "created_at": datetime(2026, 7, 20, 11, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 20, 11, tzinfo=UTC),
        "version": 1,
    }
    values.update(changes)
    return WorkoutCheckIn(**values)  # type: ignore[arg-type]


def availability_change(
    *,
    expected_version: int = 2,
    client_request_id: str = "replan-1",
    slot_count: int = 1,
) -> LocalReplanCommand:
    return LocalReplanCommand(
        client_request_id=client_request_id,
        expected_plan_version=expected_version,
        change_type=PlanChangeType.AVAILABILITY_CHANGED,
        effective_from=datetime(2026, 7, 21, 0, tzinfo=UTC),
        replacement_availability_slots=tuple(
            AvailabilitySlot(
                start=datetime(2026, 7, 23 + index, 12, tzinfo=UTC),
                end=datetime(2026, 7, 23 + index, 13, tzinfo=UTC),
                location_type=LocationType.HOME,
            )
            for index in range(slot_count)
        ),
    )


async def add_constraint(
    container: BusinessContainer,
    constraint_type: ConstraintType,
    value: str,
) -> None:
    await container.profile_service.add_constraint(
        container.development_user,
        AddConstraintCommand(
            constraint_type=constraint_type,
            constraint_value=value,
            priority=100,
            is_hard=True,
            source=ConstraintSource.USER_EXPLICIT,
            valid_until=None,
        ),
    )
