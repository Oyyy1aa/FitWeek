"""Memory may tune deterministic ordering but never hard planning rules."""

from datetime import UTC, datetime

import pytest

from app.domain.common import LocationType
from app.domain.context.enums import ContextDegradedMode
from app.domain.memory.enums import MemoryType
from app.domain.planning.models import AvailabilitySlot, GenerateWeeklyPlanCommand
from app.persistence.memory.memory_repository import InMemoryMemoryRepository
from tests.phase4b_helpers import TEST_WEEK, container, create_memory, seed_profile

pytestmark = pytest.mark.phase_4b


def command(client_request_id: str | None = None) -> GenerateWeeklyPlanCommand:
    return GenerateWeeklyPlanCommand(
        client_request_id=client_request_id,
        week_start=TEST_WEEK,
        availability_slots=(
            AvailabilitySlot(
                start=datetime(2030, 1, 7, 19, tzinfo=UTC),
                end=datetime(2030, 1, 7, 20, tzinfo=UTC),
                location_type=LocationType.HOME,
            ),
            AvailabilitySlot(
                start=datetime(2030, 1, 8, 8, tzinfo=UTC),
                end=datetime(2030, 1, 8, 9, tzinfo=UTC),
                location_type=LocationType.HOME,
            ),
            AvailabilitySlot(
                start=datetime(2030, 1, 9, 8, tzinfo=UTC),
                end=datetime(2030, 1, 9, 9, tzinfo=UTC),
                location_type=LocationType.HOME,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_time_memory_only_reorders_supplied_availability() -> None:
    value = container()
    await seed_profile(value)
    await create_memory(
        value,
        memory_type=MemoryType.PREFERRED_TIME_OF_DAY,
        key="preferred_time_of_day",
        memory_value="morning",
    )
    result = await value.plan_generation_service.generate_plan(
        value.development_user, command("memory-plan-1")
    )
    starts = {item.scheduled_start for item in result.plan.sessions}
    supplied = {item.start for item in command().availability_slots}
    assert starts <= supplied
    assert all(item.hour == 8 for item in starts)
    assert result.metadata.included_memory_count == 1
    assert result.metadata.context_snapshot_reference_id is not None
    assert result.validation.passed


@pytest.mark.asyncio
async def test_location_memory_reorders_only_supplied_availability() -> None:
    value = container()
    await seed_profile(value)
    await create_memory(
        value,
        memory_type=MemoryType.PREFERRED_LOCATION,
        key="preferred_location",
        memory_value="HOME",
    )
    request = GenerateWeeklyPlanCommand(
        client_request_id="memory-location-plan",
        week_start=TEST_WEEK,
        availability_slots=(
            AvailabilitySlot(
                start=datetime(2030, 1, 7, 8, tzinfo=UTC),
                end=datetime(2030, 1, 7, 9, tzinfo=UTC),
                location_type=LocationType.OUTDOOR,
            ),
            AvailabilitySlot(
                start=datetime(2030, 1, 8, 8, tzinfo=UTC),
                end=datetime(2030, 1, 8, 9, tzinfo=UTC),
                location_type=LocationType.HOME,
            ),
            AvailabilitySlot(
                start=datetime(2030, 1, 9, 8, tzinfo=UTC),
                end=datetime(2030, 1, 9, 9, tzinfo=UTC),
                location_type=LocationType.HOME,
            ),
        ),
    )

    result = await value.plan_generation_service.generate_plan(
        value.development_user, request
    )

    supplied = {item.start for item in request.availability_slots}
    assert {item.scheduled_start for item in result.plan.sessions} <= supplied
    assert all(item.location_type is LocationType.HOME for item in result.plan.sessions)
    assert result.validation.passed


@pytest.mark.asyncio
async def test_explicit_location_and_hard_rules_override_memory() -> None:
    value = container()
    await seed_profile(value)
    await create_memory(
        value,
        memory_type=MemoryType.PREFERRED_LOCATION,
        key="preferred_location",
        memory_value="GYM",
    )
    request = command("location-priority")
    request = GenerateWeeklyPlanCommand(
        client_request_id=request.client_request_id,
        week_start=request.week_start,
        availability_slots=request.availability_slots,
        preferred_locations=(LocationType.HOME,),
    )
    result = await value.plan_generation_service.generate_plan(
        value.development_user, request
    )
    assert all(item.location_type is LocationType.HOME for item in result.plan.sessions)
    assert result.metadata.shadowed_memory_count == 1
    assert result.validation.passed


@pytest.mark.asyncio
async def test_same_client_id_reuses_frozen_context_after_new_memory() -> None:
    value = container()
    await seed_profile(value)
    request = command("stable-client-id")
    first = await value.plan_generation_service.generate_plan(
        value.development_user, request
    )
    repeated = await value.plan_generation_service.generate_plan(
        value.development_user, request
    )
    assert first.plan == repeated.plan

    await create_memory(
        value,
        memory_type=MemoryType.PREFERRED_TIME_OF_DAY,
        key="preferred_time_of_day",
        memory_value="morning",
    )
    frozen_retry = await value.plan_generation_service.generate_plan(
        value.development_user, request
    )
    assert frozen_retry.plan == first.plan
    assert frozen_retry.metadata.context_snapshot_reference_id == (
        first.metadata.context_snapshot_reference_id
    )


@pytest.mark.asyncio
async def test_no_memory_degraded_still_generates_safe_plan() -> None:
    value = container()
    await seed_profile(value)
    repository = value.memory_repository
    assert isinstance(repository, InMemoryMemoryRepository)
    repository.set_query_failures(2)
    result = await value.plan_generation_service.generate_plan(
        value.development_user, command("no-memory-plan")
    )
    assert result.plan.status.value == "VALIDATED"
    snapshot = await value.context_application_service.get_snapshot(
        value.development_user,
        result.metadata.context_snapshot_reference_id,
    )
    assert snapshot.reference.degraded_mode is ContextDegradedMode.NO_MEMORY
