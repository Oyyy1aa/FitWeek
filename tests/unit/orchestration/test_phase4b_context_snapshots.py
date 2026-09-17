"""Both orchestrator workflows checkpoint only frozen Context references."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.common import LocationType
from app.domain.memory.enums import MemoryType
from app.domain.orchestration.enums import PlanningRunStatus
from app.domain.planning.models import AvailabilitySlot, GenerateWeeklyPlanCommand
from tests.phase4b_helpers import container, create_memory, seed_profile

pytestmark = pytest.mark.phase_4b


def planning_command(week: date) -> GenerateWeeklyPlanCommand:
    return GenerateWeeklyPlanCommand(
        week_start=week,
        availability_slots=tuple(
            AvailabilitySlot(
                start=datetime.combine(
                    week + timedelta(days=offset),
                    datetime.min.time(),
                    tzinfo=UTC,
                ).replace(hour=9),
                end=datetime.combine(
                    week + timedelta(days=offset),
                    datetime.min.time(),
                    tzinfo=UTC,
                ).replace(hour=10),
                location_type=LocationType.HOME,
            )
            for offset in (0, 2)
        ),
    )


@pytest.mark.asyncio
async def test_plan_run_freezes_snapshot_while_new_run_sees_new_memory() -> None:
    value = container(orchestrator=True)
    await seed_profile(value)
    user = value.development_user
    first = await value.orchestration_service.create_run(
        user,
        client_request_id="context-run-a",
        command=planning_command(date(2030, 1, 7)),
    )
    worker = value.orchestrator_workers[0]
    assert (await worker.run_once()).claimed
    checkpoints = await value.orchestration_service.list_checkpoints(user, first.run.id)
    load_output = checkpoints[0].output_payload
    assert "snapshot_reference_id" in load_output
    assert "HOME" not in repr(load_output)

    await create_memory(
        value,
        memory_type=MemoryType.PREFERRED_LOCATION,
        key="preferred_location",
        memory_value="HOME",
    )
    assert (await worker.run_once()).claimed
    first_plan = (await value.plan_service.list_plans(user))[0]
    assert first_plan.generation_metadata is not None
    assert first_plan.generation_metadata["included_memory_count"] == "0"
    assert (await worker.run_once()).claimed
    assert (await worker.run_once()).claimed

    second = await value.orchestration_service.create_run(
        user,
        client_request_id="context-run-b",
        command=planning_command(date(2030, 1, 14)),
    )
    assert (await worker.run_once()).claimed
    second_checkpoint = (
        await value.orchestration_service.list_checkpoints(user, second.run.id)
    )[0]
    assert second_checkpoint.output_payload["included_memory_count"] == 1
    assert (
        second_checkpoint.output_payload["snapshot_reference_id"]
        != (load_output["snapshot_reference_id"])
    )


@pytest.mark.asyncio
async def test_profile_run_checkpoints_reference_not_memory_text() -> None:
    value = container(orchestrator=True)
    await seed_profile(value)
    await create_memory(
        value,
        memory_type=MemoryType.PREFERRED_LOCATION,
        key="preferred_location",
        memory_value="HOME",
    )
    created = await value.profile_agent_run_service.create_run(
        value.development_user,
        client_request_id="profile-context-run",
        user_message="I want a general fitness profile at home.",
        current_week=date(2030, 1, 7),
    )
    worker = value.orchestrator_workers[0]
    assert (await worker.run_once()).claimed
    assert (await worker.run_once()).claimed
    run = await value.profile_agent_run_service.get_run(
        value.development_user, created.run.id
    )
    assert run.status is PlanningRunStatus.WAITING_PROFILE_REVIEW
    checkpoints = await value.profile_agent_run_service.list_checkpoints(
        value.development_user, run.id
    )
    assert checkpoints[0].output_payload["included_memory_count"] == 1
    assert "snapshot_reference_id" in checkpoints[0].output_payload
    assert "HOME" not in repr(checkpoints[0].output_payload)
