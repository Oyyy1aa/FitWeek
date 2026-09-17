"""Reusable Phase 4A service and fixture helpers."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.api.dependencies import BusinessContainer, build_memory_container
from app.application.profiles import AddConstraintCommand, UpsertProfileCommand
from app.config import Settings
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
)


def memory_container() -> BusinessContainer:
    return build_memory_container(
        Settings(
            persistence_backend="memory",
            redis_enabled=False,
            orchestrator_enabled=False,
            model_gateway_enabled=False,
            context_max_characters=8000,
            context_max_memories=10,
            context_max_behavior_items=10,
        )
    )


async def seed_profile(
    container: BusinessContainer,
    *,
    primary_goal: FitnessGoal = FitnessGoal.GENERAL_FITNESS,
) -> None:
    await container.profile_service.upsert_profile(
        container.development_user,
        UpsertProfileCommand(
            experience_level=ExperienceLevel.BEGINNER,
            weekly_frequency=3,
            max_session_minutes=30,
            primary_goal=primary_goal,
            scope_confirmed=True,
            expected_version=None,
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


def future(hours: int = 24) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


def other_user_id() -> UUID:
    return uuid4()
