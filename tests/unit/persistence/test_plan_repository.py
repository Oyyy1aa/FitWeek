"""Snapshot, locking, and ownership behavior of the in-memory plan repository."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.persistence.memory.plan_repository import InMemoryPlanRepository
from app.persistence.memory.store import InMemoryStore
from tests.factories import TEST_WEEK_START, make_plan

pytestmark = pytest.mark.phase_1a


@pytest.mark.asyncio
async def test_plan_save_and_reads_are_deep_snapshot_isolated() -> None:
    repository = InMemoryPlanRepository(InMemoryStore())
    plan = make_plan()

    returned = await repository.save(plan)
    plan.goal_snapshot["primary_goal"] = "MUTATED_INPUT"
    returned.goal_snapshot["primary_goal"] = "MUTATED_RESULT"
    first_read = await repository.get(plan.id)
    assert first_read is not None
    first_read.goal_snapshot["primary_goal"] = "MUTATED_READ"
    second_read = await repository.get(plan.id)

    assert second_read is not None
    assert second_read.goal_snapshot == {"primary_goal": "GENERAL_FITNESS"}
    assert second_read.sessions is not plan.sessions


@pytest.mark.asyncio
async def test_plan_update_rejects_stale_version_and_accepts_next_version() -> None:
    repository = InMemoryPlanRepository(InMemoryStore())
    original = make_plan()
    saved = await repository.save(original)

    updated = replace(saved, version=2, goal_snapshot={"primary_goal": "MIXED"})
    assert await repository.save(updated) == updated

    stale = replace(saved, version=2, goal_snapshot={"primary_goal": "STALE"})
    with pytest.raises(RepositoryConflictError) as captured:
        await repository.save(stale)

    assert captured.value.expected_version == 3
    assert captured.value.actual_version == 2


@pytest.mark.asyncio
async def test_user_week_revision_is_unique() -> None:
    repository = InMemoryPlanRepository(InMemoryStore())
    user_id = uuid4()
    first = make_plan(user_id=user_id)
    duplicate = make_plan(user_id=user_id)
    await repository.save(first)

    with pytest.raises(RepositoryUniqueError) as captured:
        await repository.save(duplicate)

    assert captured.value.constraint == "weekly_plan.user_week_revision"


@pytest.mark.asyncio
async def test_concurrent_plan_save_has_one_unique_winner() -> None:
    repository = InMemoryPlanRepository(InMemoryStore())
    user_id = uuid4()
    candidates = [make_plan(user_id=user_id), make_plan(user_id=user_id)]

    outcomes = await asyncio.gather(
        *(repository.save(candidate) for candidate in candidates),
        return_exceptions=True,
    )

    saved = [item for item in outcomes if not isinstance(item, BaseException)]
    conflicts = [item for item in outcomes if isinstance(item, RepositoryUniqueError)]
    assert len(saved) == 1
    assert len(conflicts) == 1
    assert await repository.list_by_user(user_id) == saved


@pytest.mark.asyncio
async def test_plan_and_session_queries_are_user_scoped() -> None:
    repository = InMemoryPlanRepository(InMemoryStore())
    owner_id = uuid4()
    other_id = uuid4()
    plan = make_plan(user_id=owner_id)
    await repository.save(plan)
    session_id = plan.sessions[0].id

    assert await repository.get_for_user(plan.id, owner_id) == plan
    assert await repository.get_for_user(plan.id, other_id) is None
    assert await repository.list_by_user(owner_id) == [plan]
    assert await repository.list_by_user(other_id) == []
    assert await repository.list_sessions_for_user(plan.id, owner_id) == list(
        plan.sessions
    )
    assert await repository.list_sessions_for_user(plan.id, other_id) == []
    assert (
        await repository.get_session_for_user(session_id, owner_id) == plan.sessions[0]
    )
    assert await repository.get_session_for_user(session_id, other_id) is None


@pytest.mark.asyncio
async def test_plan_lists_have_stable_chronological_order() -> None:
    repository = InMemoryPlanRepository(InMemoryStore())
    user_id = uuid4()
    later = make_plan(user_id=user_id, week_start=TEST_WEEK_START + timedelta(days=7))
    earlier = make_plan(user_id=user_id, week_start=TEST_WEEK_START)
    await repository.save(later)
    await repository.save(earlier)

    assert await repository.list_by_user(user_id) == [earlier, later]


@pytest.mark.asyncio
async def test_store_reset_clears_plans_and_session_index() -> None:
    store = InMemoryStore()
    repository = InMemoryPlanRepository(store)
    plan = make_plan()
    await repository.save(plan)

    await store.reset()

    assert await repository.get(plan.id) is None
    assert await repository.get_session(plan.sessions[0].id) is None
    assert await repository.list_by_user(plan.user_id) == []
