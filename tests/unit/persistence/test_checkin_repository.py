"""Atomic and isolated behavior of the process-local check-in adapter."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from app.domain.common import RepositoryUniqueError
from app.persistence.memory import InMemoryCheckInRepository, InMemoryStore
from tests.phase1a2_helpers import make_check_in

pytestmark = pytest.mark.phase_1a2


@pytest.mark.asyncio
async def test_save_read_and_deep_copy_isolation() -> None:
    repository = InMemoryCheckInRepository(InMemoryStore())
    check_in = make_check_in()

    saved = await repository.save(check_in)
    first = await repository.get(saved.id)
    second = await repository.get_by_session_id(saved.session_id)

    assert first == second == check_in
    assert first is not second


@pytest.mark.asyncio
async def test_same_idempotency_key_and_payload_reuses_record() -> None:
    repository = InMemoryCheckInRepository(InMemoryStore())
    check_in = make_check_in()

    first = await repository.save(check_in)
    second = await repository.save(replace(check_in, id=uuid4()))

    assert second == first


@pytest.mark.asyncio
async def test_same_idempotency_key_with_other_payload_conflicts() -> None:
    repository = InMemoryCheckInRepository(InMemoryStore())
    check_in = make_check_in()
    await repository.save(check_in)

    with pytest.raises(RepositoryUniqueError):
        await repository.save(replace(check_in, actual_minutes=20))


@pytest.mark.asyncio
async def test_session_accepts_only_one_effective_check_in() -> None:
    repository = InMemoryCheckInRepository(InMemoryStore())
    check_in = make_check_in()
    await repository.save(check_in)

    with pytest.raises(RepositoryUniqueError):
        await repository.save(
            replace(check_in, id=uuid4(), client_event_id="other-event")
        )


@pytest.mark.asyncio
async def test_concurrent_identical_events_create_one_record() -> None:
    repository = InMemoryCheckInRepository(InMemoryStore())
    check_in = make_check_in()

    results = await asyncio.gather(
        *(repository.save(replace(check_in, id=uuid4())) for _ in range(8))
    )

    assert len({item.id for item in results}) == 1
    assert len(await repository.list_by_series(check_in.plan_id)) == 1


@pytest.mark.asyncio
async def test_list_sort_and_reset_are_stable() -> None:
    store = InMemoryStore()
    repository = InMemoryCheckInRepository(store)
    first = make_check_in()
    second = make_check_in(
        client_event_id="earlier",
        plan_id=first.plan_id,
        session_id=uuid4(),
        occurred_at=first.occurred_at.replace(hour=10),
    )
    await repository.save(first)
    await repository.save(second)

    assert await repository.list_by_series(first.plan_id) == [second, first]
    await repository.clear()
    assert await repository.list_by_series(first.plan_id) == []
