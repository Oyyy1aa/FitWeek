"""Application orchestration for deterministic plan generation."""

from datetime import UTC, datetime

import pytest

from app.api.dependencies import build_memory_container
from app.application.errors import (
    ConflictError,
    PlanGenerationFailed,
    ResourceNotFound,
)
from app.application.plan_generation import PlanGenerationService
from app.application.profiles import UpsertProfileCommand
from app.domain.common import LocationType
from app.domain.planning.models import AvailabilitySlot, GenerateWeeklyPlanCommand
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.profiles.models import ExperienceLevel, FitnessGoal
from app.safety.models import SafetyValidationResult, SafetyViolation
from tests.factories import TEST_WEEK_START, make_plan

pytestmark = pytest.mark.phase_1a1


def generation_command(*, slot_count: int = 2) -> GenerateWeeklyPlanCommand:
    return GenerateWeeklyPlanCommand(
        week_start=TEST_WEEK_START,
        availability_slots=tuple(
            AvailabilitySlot(
                start=datetime(2026, 7, 20 + index * 2, 10, tzinfo=UTC),
                end=datetime(2026, 7, 20 + index * 2, 11, tzinfo=UTC),
                location_type=LocationType.HOME,
            )
            for index in range(slot_count)
        ),
    )


async def create_profile(container, *, scope_confirmed: bool = True) -> None:
    await container.profile_service.upsert_profile(
        container.development_user,
        UpsertProfileCommand(
            experience_level=ExperienceLevel.BEGINNER,
            weekly_frequency=2,
            max_session_minutes=45,
            primary_goal=FitnessGoal.GENERAL_FITNESS,
            scope_confirmed=scope_confirmed,
        ),
    )


@pytest.mark.asyncio
async def test_missing_profile_is_reported_and_nothing_is_saved() -> None:
    container = build_memory_container()

    with pytest.raises(ResourceNotFound):
        await container.plan_generation_service.generate_plan(
            container.development_user,
            generation_command(),
        )

    assert (
        await container.plan_repository.list_by_user(container.development_user.id)
        == []
    )


@pytest.mark.asyncio
async def test_unconfirmed_scope_is_not_repaired_or_saved() -> None:
    container = build_memory_container()
    await create_profile(container, scope_confirmed=False)

    with pytest.raises(PlanGenerationFailed) as captured:
        await container.plan_generation_service.generate_plan(
            container.development_user,
            generation_command(),
        )

    assert {item.code for item in captured.value.reasons} == {"SCOPE_NOT_CONFIRMED"}
    assert (
        await container.plan_repository.list_by_user(container.development_user.id)
        == []
    )


@pytest.mark.asyncio
async def test_safe_generation_is_saved_validated_and_queryable() -> None:
    container = build_memory_container()
    await create_profile(container)

    result = await container.plan_generation_service.generate_plan(
        container.development_user,
        generation_command(),
    )

    assert result.validation.passed is True
    assert result.plan.status is WeeklyPlanStatus.VALIDATED
    assert result.plan.generation_metadata == result.metadata.as_dict()
    assert await container.plan_service.list_plans(container.development_user) == [
        result.plan
    ]


@pytest.mark.asyncio
async def test_repeated_generation_reuses_deterministic_saved_plan() -> None:
    container = build_memory_container()
    await create_profile(container)
    command = generation_command()

    first = await container.plan_generation_service.generate_plan(
        container.development_user, command
    )
    second = await container.plan_generation_service.generate_plan(
        container.development_user, command
    )

    assert first.plan == second.plan
    assert (
        len(await container.plan_repository.list_by_user(container.development_user.id))
        == 1
    )


@pytest.mark.asyncio
async def test_infeasible_generation_does_not_save_partial_plan() -> None:
    container = build_memory_container()
    await create_profile(container)

    with pytest.raises(PlanGenerationFailed) as captured:
        await container.plan_generation_service.generate_plan(
            container.development_user,
            generation_command(slot_count=1),
        )

    assert "INSUFFICIENT_AVAILABILITY" in {item.code for item in captured.value.reasons}
    assert (
        await container.plan_repository.list_by_user(container.development_user.id)
        == []
    )


@pytest.mark.asyncio
async def test_repository_revision_conflict_is_mapped_to_application_conflict() -> None:
    container = build_memory_container()
    await create_profile(container)
    existing = make_plan(
        user_id=container.development_user.id,
        status=WeeklyPlanStatus.VALIDATED,
    )
    await container.plan_repository.save(existing)

    with pytest.raises(ConflictError):
        await container.plan_generation_service.generate_plan(
            container.development_user,
            generation_command(),
        )

    assert (
        len(await container.plan_repository.list_by_user(container.development_user.id))
        == 1
    )


class AlwaysUnsafe:
    calls = 0

    def validate_plan(self, **_):
        self.calls += 1
        return SafetyValidationResult.from_violations(
            (
                SafetyViolation(
                    code="EQUIPMENT_MISMATCH",
                    message="Required equipment is unavailable.",
                ),
            )
        )


class CountingRepairer:
    calls = 0

    def repair(self, *, plan, **_):
        self.calls += 1
        return plan


@pytest.mark.asyncio
async def test_repair_is_bounded_to_two_attempts_and_failure_is_not_saved() -> None:
    container = build_memory_container()
    await create_profile(container)
    safety = AlwaysUnsafe()
    repairer = CountingRepairer()
    service = PlanGenerationService(
        profiles=container.profile_repository,
        exercises=container.exercise_repository,
        plans=container.plan_repository,
        safety_engine=safety,
        repairer=repairer,
    )

    with pytest.raises(PlanGenerationFailed):
        await service.generate_plan(container.development_user, generation_command())

    assert repairer.calls == 2
    assert safety.calls == 3
    assert (
        await container.plan_repository.list_by_user(container.development_user.id)
        == []
    )
