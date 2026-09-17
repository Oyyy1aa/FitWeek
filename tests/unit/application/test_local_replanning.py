"""Local revision orchestration, lineage, safety, and idempotency."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.application.errors import (
    IdempotencyConflict,
    LocalReplanningFailed,
    PlanVersionConflict,
)
from app.domain.checkins.models import CheckInStatus
from app.domain.replanning.models import LocalReplanCommand, PlanChangeType
from tests.phase1a2_helpers import (
    availability_change,
    check_in_command,
    prepared_container,
)

pytestmark = pytest.mark.phase_1a2


async def create_revision(*, with_check_in: bool = True):
    container, source = await prepared_container()
    if with_check_in:
        await container.check_in_service.create(
            container.development_user,
            source.sessions[0].id,
            check_in_command(),
        )
    result = await container.local_replanning_service.replan(
        container.development_user,
        source.id,
        availability_change(),
    )
    return container, source, result


@pytest.mark.asyncio
async def test_only_affected_future_session_is_rebuilt() -> None:
    _, source, result = await create_revision()
    revised = result.plan

    assert revised.revision == source.revision + 1
    assert revised.status.value == "VALIDATED"
    assert revised.confirmed_at is None
    assert revised.sessions[0] == source.sessions[0]
    assert revised.sessions[1].id != source.sessions[1].id
    assert revised.sessions[1].scheduled_start == datetime(2026, 7, 23, 12, tzinfo=UTC)
    assert revised.change_metadata is not None
    assert revised.change_metadata.immutable_session_ids == (source.sessions[0].id,)
    assert revised.change_metadata.changed_session_ids == (source.sessions[1].id,)


@pytest.mark.asyncio
async def test_original_revision_is_retained_and_revision_is_queryable() -> None:
    container, source, result = await create_revision()

    revisions = await container.local_replanning_service.list_revisions(
        container.development_user, source.id
    )

    assert revisions == [source, result.plan]
    assert await container.plan_repository.get(source.id) == source


@pytest.mark.asyncio
async def test_replan_is_idempotent_and_does_not_create_extra_revision() -> None:
    container, source, first = await create_revision()
    second = await container.local_replanning_service.replan(
        container.development_user,
        source.id,
        availability_change(),
    )

    assert first.plan == second.plan
    assert second.created is False
    assert (
        len(
            await container.local_replanning_service.list_revisions(
                container.development_user, source.id
            )
        )
        == 2
    )


@pytest.mark.asyncio
async def test_same_request_id_with_different_payload_conflicts() -> None:
    container, source, _ = await create_revision()

    with pytest.raises(IdempotencyConflict):
        await container.local_replanning_service.replan(
            container.development_user,
            source.id,
            replace(
                availability_change(),
                replacement_availability_slots=(
                    replace(
                        availability_change().replacement_availability_slots[0],
                        start=datetime(2026, 7, 24, 12, tzinfo=UTC),
                        end=datetime(2026, 7, 24, 13, tzinfo=UTC),
                    ),
                ),
            ),
        )


@pytest.mark.asyncio
async def test_stale_expected_version_conflicts_without_saving() -> None:
    container, source = await prepared_container()
    before = await container.plan_repository.list_by_user(container.development_user.id)

    with pytest.raises(PlanVersionConflict):
        await container.local_replanning_service.replan(
            container.development_user,
            source.id,
            availability_change(expected_version=1),
        )

    assert (
        await container.plan_repository.list_by_user(container.development_user.id)
        == before
    )


@pytest.mark.asyncio
async def test_insufficient_slots_fail_without_saving() -> None:
    container, source = await prepared_container(weekly_frequency=3)
    before = await container.plan_repository.list_by_user(container.development_user.id)
    command = LocalReplanCommand(
        client_request_id="too-few",
        expected_plan_version=source.version,
        change_type=PlanChangeType.AVAILABILITY_CHANGED,
        effective_from=datetime(2026, 7, 20, 0, tzinfo=UTC),
        replacement_availability_slots=(
            availability_change().replacement_availability_slots[0],
        ),
    )

    with pytest.raises(LocalReplanningFailed) as captured:
        await container.local_replanning_service.replan(
            container.development_user, source.id, command
        )

    assert {item.code for item in captured.value.reasons} == {
        "INSUFFICIENT_REPLACEMENT_SLOTS"
    }
    assert (
        await container.plan_repository.list_by_user(container.development_user.id)
        == before
    )


@pytest.mark.asyncio
async def test_checked_session_change_is_an_immutable_conflict() -> None:
    container, source = await prepared_container()
    await container.check_in_service.create(
        container.development_user,
        source.sessions[0].id,
        check_in_command(
            status=CheckInStatus.COMPLETED,
            actual_minutes=30,
        ),
    )
    command = LocalReplanCommand(
        client_request_id="immutable",
        expected_plan_version=source.version,
        change_type=PlanChangeType.SESSION_DURATION_CHANGED,
        effective_from=datetime(2026, 7, 20, 0, tzinfo=UTC),
        max_session_minutes=15,
    )

    with pytest.raises(LocalReplanningFailed) as captured:
        await container.local_replanning_service.replan(
            container.development_user, source.id, command
        )

    assert {item.code for item in captured.value.reasons} == {
        "IMMUTABLE_SESSION_CONFLICT"
    }


@pytest.mark.asyncio
async def test_revision_confirmation_preserves_old_revision_and_becomes_current() -> (
    None
):
    container, source, result = await create_revision()

    confirmed = await container.local_replanning_service.confirm_revision(
        container.development_user,
        source.id,
        result.plan.revision,
        expected_version=result.plan.version,
    )
    old = await container.local_replanning_service.get_revision(
        container.development_user, source.id, 1
    )

    assert confirmed.status.value == "CONFIRMED"
    assert old == source
    assert await container.local_replanning_service.is_current_revision(
        container.development_user, confirmed
    )
    assert not await container.local_replanning_service.is_current_revision(
        container.development_user, old
    )


@pytest.mark.asyncio
async def test_identical_request_stays_idempotent_after_revision_confirmation() -> None:
    container, source, result = await create_revision()
    await container.local_replanning_service.confirm_revision(
        container.development_user,
        source.id,
        result.plan.revision,
        expected_version=result.plan.version,
    )

    repeated = await container.local_replanning_service.replan(
        container.development_user, source.id, availability_change()
    )

    assert repeated.plan.id == result.plan.id
    assert repeated.created is False


class AlwaysUnsafe:
    def __init__(self) -> None:
        self.calls = 0

    def validate_plan(self, **_: object):
        from app.safety.models import SafetyValidationResult, SafetyViolation

        self.calls += 1
        return SafetyValidationResult.from_violations(
            (SafetyViolation(code="TEST_UNSAFE", message="unsafe"),)
        )


@pytest.mark.asyncio
async def test_safety_repair_is_bounded_to_two_and_failure_is_not_saved() -> None:
    container, source = await prepared_container()
    always_unsafe = AlwaysUnsafe()
    container.local_replanning_service._safety = always_unsafe  # type: ignore[assignment]
    before = await container.plan_repository.list_by_user(container.development_user.id)

    with pytest.raises(LocalReplanningFailed):
        await container.local_replanning_service.replan(
            container.development_user, source.id, availability_change()
        )

    assert (
        await container.plan_repository.list_by_user(container.development_user.id)
        == before
    )
    assert always_unsafe.calls == 3
