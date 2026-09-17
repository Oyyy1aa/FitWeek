"""Controlled-catalog behavior of the process-local exercise repository."""

from dataclasses import replace

import pytest

from app.domain.common import LocationType, RepositoryUniqueError
from app.domain.exercises.catalog_seed import EXERCISE_CATALOG_SEED
from app.domain.exercises.models import ExerciseStatus
from app.persistence.memory.exercise_repository import InMemoryExerciseRepository
from app.persistence.memory.store import InMemoryStore

pytestmark = pytest.mark.phase_1a


@pytest.mark.asyncio
async def test_active_catalog_is_sorted_and_disabled_entries_are_hidden() -> None:
    repository = InMemoryExerciseRepository(InMemoryStore(EXERCISE_CATALOG_SEED))

    active = await repository.list_active()

    assert [exercise.id for exercise in active] == sorted(
        exercise.id for exercise in active
    )
    assert all(exercise.status is ExerciseStatus.ACTIVE for exercise in active)
    assert "burpee" not in {exercise.id for exercise in active}
    disabled = await repository.get("burpee")
    assert disabled is not None
    assert disabled.status is ExerciseStatus.DISABLED


@pytest.mark.asyncio
async def test_catalog_filters_by_location_and_explicit_equipment() -> None:
    repository = InMemoryExerciseRepository(InMemoryStore(EXERCISE_CATALOG_SEED))

    outdoor_without_equipment = await repository.list_active(
        location=LocationType.OUTDOOR,
        equipment=set(),
    )
    home_with_band = await repository.list_active(
        location=LocationType.HOME,
        equipment={"resistance_band"},
    )

    assert outdoor_without_equipment
    assert all(
        LocationType.OUTDOOR in exercise.location_types
        and not exercise.required_equipment
        for exercise in outdoor_without_equipment
    )
    assert "resistance_band_row" in {exercise.id for exercise in home_with_band}
    assert all(
        exercise.required_equipment <= {"resistance_band"}
        for exercise in home_with_band
    )


@pytest.mark.asyncio
async def test_catalog_seed_rejects_duplicate_ids_atomically() -> None:
    repository = InMemoryExerciseRepository(InMemoryStore(EXERCISE_CATALOG_SEED))
    existing = EXERCISE_CATALOG_SEED[0]
    distinct = replace(existing, id="new_unique_exercise", name="New Exercise")

    with pytest.raises(RepositoryUniqueError):
        await repository.seed((distinct, existing))

    assert await repository.get(distinct.id) is None


@pytest.mark.asyncio
async def test_store_reset_restores_constructor_catalog_and_runtime_state() -> None:
    store = InMemoryStore(EXERCISE_CATALOG_SEED)
    repository = InMemoryExerciseRepository(store)
    await repository.clear()
    assert await repository.list_active() == []

    await store.reset()

    restored = await repository.list_active()
    assert len(restored) == sum(
        exercise.status is ExerciseStatus.ACTIVE for exercise in EXERCISE_CATALOG_SEED
    )
