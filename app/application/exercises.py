"""Read-only exercise catalog use cases."""

from app.application.errors import ResourceNotFound
from app.domain.common import LocationType
from app.domain.exercises.models import Exercise
from app.domain.exercises.repositories import ExerciseRepository


class ExerciseCatalogService:
    """Expose only reviewed, repository-backed exercise records."""

    def __init__(self, repository: ExerciseRepository) -> None:
        self._repository = repository

    async def get_exercise(self, exercise_id: str) -> Exercise:
        exercise = await self._repository.get(exercise_id)
        if exercise is None or exercise.status.value != "ACTIVE":
            raise ResourceNotFound("Exercise was not found.")
        return exercise

    async def list_exercises(
        self,
        *,
        location: LocationType | None = None,
        equipment: set[str] | None = None,
    ) -> list[Exercise]:
        normalized_equipment = (
            None
            if equipment is None
            else {item.strip().lower() for item in equipment if item.strip()}
        )
        return await self._repository.list_active(location, normalized_equipment)
