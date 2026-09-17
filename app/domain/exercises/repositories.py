"""Persistence-independent exercise catalog contract."""

from typing import Protocol

from app.domain.common import LocationType
from app.domain.exercises.models import Exercise


class ExerciseRepository(Protocol):
    async def get(self, exercise_id: str) -> Exercise | None: ...

    async def list_active(
        self,
        location: LocationType | None = None,
        equipment: set[str] | None = None,
    ) -> list[Exercise]: ...
