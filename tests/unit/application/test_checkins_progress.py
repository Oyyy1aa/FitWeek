"""Check-in orchestration and exact progress arithmetic."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.api.dependencies import build_memory_container
from app.application.errors import BusinessRuleViolation, IdempotencyConflict
from app.application.profiles import UpsertProfileCommand
from app.application.progress import ProgressService
from app.domain.checkins.models import CheckInStatus
from app.domain.profiles.models import ExperienceLevel, FitnessGoal
from tests.phase1a2_helpers import (
    check_in_command,
    generation_command,
    prepared_container,
)

pytestmark = pytest.mark.phase_1a2


@pytest.mark.asyncio
async def test_confirmed_session_check_in_is_idempotent() -> None:
    container, plan = await prepared_container()
    session_id = plan.sessions[0].id
    command = check_in_command()

    first = await container.check_in_service.create(
        container.development_user, session_id, command
    )
    second = await container.check_in_service.create(
        container.development_user, session_id, command
    )

    assert first.created is True
    assert second.created is False
    assert first.check_in == second.check_in


@pytest.mark.asyncio
async def test_conflicting_idempotency_payload_is_rejected() -> None:
    container, plan = await prepared_container()
    command = check_in_command()
    await container.check_in_service.create(
        container.development_user, plan.sessions[0].id, command
    )

    with pytest.raises(IdempotencyConflict):
        await container.check_in_service.create(
            container.development_user,
            plan.sessions[0].id,
            replace(command, actual_minutes=15),
        )


@pytest.mark.asyncio
async def test_unconfirmed_session_cannot_be_checked_in() -> None:
    container = build_memory_container()
    await container.profile_service.upsert_profile(
        container.development_user,
        UpsertProfileCommand(
            experience_level=ExperienceLevel.BEGINNER,
            weekly_frequency=2,
            max_session_minutes=45,
            primary_goal=FitnessGoal.GENERAL_FITNESS,
            scope_confirmed=True,
        ),
    )
    generated = await container.plan_generation_service.generate_plan(
        container.development_user,
        generation_command(),
    )

    with pytest.raises(BusinessRuleViolation):
        await container.check_in_service.create(
            container.development_user,
            generated.plan.sessions[0].id,
            check_in_command(client_event_id="unconfirmed"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "expected_rate", "actual"),
    [
        ((), Decimal("0.0000"), 0),
        ((CheckInStatus.COMPLETED,), Decimal("0.5000"), 30),
        ((CheckInStatus.PARTIALLY_COMPLETED,), Decimal("0.2500"), 10),
        ((CheckInStatus.SKIPPED,), Decimal("0.0000"), 0),
        (
            (CheckInStatus.COMPLETED, CheckInStatus.PARTIALLY_COMPLETED),
            Decimal("0.7500"),
            40,
        ),
    ],
)
async def test_progress_summary_is_deterministic_and_non_medical(
    statuses: tuple[CheckInStatus, ...],
    expected_rate: Decimal,
    actual: int,
) -> None:
    container, plan = await prepared_container()
    for index, status in enumerate(statuses):
        minutes = 30 if status is CheckInStatus.COMPLETED else 10
        if status is CheckInStatus.SKIPPED:
            minutes = None
        await container.check_in_service.create(
            container.development_user,
            plan.sessions[index].id,
            check_in_command(
                client_event_id=f"event-{index}",
                status=status,
                actual_minutes=minutes,
            ),
        )

    summary = await ProgressService(
        plans=container.plan_repository,
        check_ins=container.check_in_repository,
    ).summarize(container.development_user, plan.id)

    assert summary.completion_rate == expected_rate
    assert summary.actual_minutes == actual
    assert not hasattr(summary, "medical_assessment")
