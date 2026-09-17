"""Contract behavior of the process-local profile repository."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.profiles.models import ConstraintType
from app.persistence.memory.profile_repository import InMemoryProfileRepository
from app.persistence.memory.store import InMemoryStore
from tests.factories import TEST_NOW, make_constraint, make_profile

pytestmark = pytest.mark.phase_1a


@pytest.mark.asyncio
async def test_one_profile_per_user_is_enforced() -> None:
    repository = InMemoryProfileRepository(InMemoryStore())
    user_id = uuid4()
    first = make_profile(user_id=user_id)
    duplicate = make_profile(user_id=user_id)
    await repository.save(first)

    with pytest.raises(RepositoryUniqueError) as captured:
        await repository.save(duplicate)

    assert captured.value.constraint == "fitness_profile.user_id"
    assert await repository.get_by_user_id(user_id) == first


@pytest.mark.asyncio
async def test_profile_update_requires_exact_next_version() -> None:
    repository = InMemoryProfileRepository(InMemoryStore())
    original = make_profile()
    await repository.save(original)

    with pytest.raises(RepositoryConflictError) as captured:
        await repository.save(replace(original, weekly_frequency=3))

    assert captured.value.expected_version == 2
    assert captured.value.actual_version == 1

    updated = replace(original, weekly_frequency=3, version=2, updated_at=TEST_NOW)
    assert await repository.save(updated) == updated


@pytest.mark.asyncio
async def test_constraint_list_is_stably_sorted_and_delete_is_profile_scoped() -> None:
    repository = InMemoryProfileRepository(InMemoryStore())
    profile = make_profile()
    other_profile = make_profile()
    await repository.save(profile)
    await repository.save(other_profile)
    lower = make_constraint(
        profile.id,
        ConstraintType.EXCLUDED_FEATURE,
        "jumping",
        priority=10,
    )
    higher = make_constraint(
        profile.id,
        ConstraintType.AVAILABLE_EQUIPMENT,
        "resistance_band",
        priority=100,
    )
    await repository.add_constraint(lower)
    await repository.add_constraint(higher)

    assert await repository.list_constraints(profile.id) == [higher, lower]
    assert not await repository.delete_constraint(other_profile.id, higher.id)
    assert await repository.delete_constraint(profile.id, higher.id)
    assert await repository.list_constraints(profile.id) == [lower]


@pytest.mark.asyncio
async def test_profile_repository_clear_removes_profiles_and_constraints() -> None:
    repository = InMemoryProfileRepository(InMemoryStore())
    profile = make_profile()
    constraint = make_constraint(
        profile.id,
        ConstraintType.EXCLUDED_FEATURE,
        "running",
    )
    await repository.save(profile)
    await repository.add_constraint(constraint)

    await repository.clear()

    assert await repository.get_by_user_id(profile.user_id) is None
    assert await repository.list_constraints(profile.id) == []


@pytest.mark.asyncio
async def test_concurrent_profile_creation_preserves_unique_user_boundary() -> None:
    repository = InMemoryProfileRepository(InMemoryStore())
    user_id = uuid4()
    candidates = [make_profile(user_id=user_id), make_profile(user_id=user_id)]

    outcomes = await asyncio.gather(
        *(repository.save(candidate) for candidate in candidates),
        return_exceptions=True,
    )

    saved = [item for item in outcomes if not isinstance(item, BaseException)]
    conflicts = [item for item in outcomes if isinstance(item, RepositoryUniqueError)]
    assert len(saved) == 1
    assert len(conflicts) == 1
    assert await repository.get_by_user_id(user_id) == saved[0]
