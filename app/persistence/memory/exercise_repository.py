"""Read-only in-memory adapter for the controlled exercise catalog."""

from collections.abc import Iterable
from copy import deepcopy

from app.domain.common import LocationType
from app.domain.exercises.models import Exercise, ExerciseStatus
from app.persistence.memory.store import InMemoryStore, RepositoryUniqueError


class InMemoryExerciseRepository:
    """Query catalog snapshots without exposing the store's mutable mappings."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get(self, exercise_id: str) -> Exercise | None:
        async with self._store.lock:
            exercise = self._store._exercises.get(exercise_id)
            return deepcopy(exercise)

    async def list_active(
        self,
        location: LocationType | None = None,
        equipment: set[str] | None = None,
    ) -> list[Exercise]:
        available_equipment = None if equipment is None else frozenset(equipment)
        async with self._store.lock:
            matches = [
                exercise
                for exercise in self._store._exercises.values()
                if exercise.status is ExerciseStatus.ACTIVE
                and (location is None or location in exercise.location_types)
                and (
                    available_equipment is None
                    or exercise.required_equipment <= available_equipment
                )
            ]
            matches.sort(key=lambda item: item.id)
            return deepcopy(matches)

    async def seed(self, exercises: Iterable[Exercise]) -> None:
        """Add controlled bootstrap entries while preserving ID uniqueness."""

        snapshots = deepcopy(tuple(exercises))
        async with self._store.lock:
            incoming_ids: set[str] = set()
            for exercise in snapshots:
                if exercise.id in incoming_ids or exercise.id in self._store._exercises:
                    raise RepositoryUniqueError("exercise.id", exercise.id)
                incoming_ids.add(exercise.id)
            for exercise in snapshots:
                self._store._exercises[exercise.id] = exercise
            self._store._seed_exercises = (
                *self._store._seed_exercises,
                *deepcopy(snapshots),
            )

    async def clear(self) -> None:
        """Clear catalog entries for isolated repository tests."""

        async with self._store.lock:
            self._store._exercises.clear()
