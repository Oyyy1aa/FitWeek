"""Controlled MySQL exercise catalog adapter and repeatable bootstrap seed."""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.common import LocationType
from app.domain.exercises.models import (
    Exercise,
    ExerciseDifficulty,
    ExerciseStatus,
)
from app.persistence.mysql.models import ExerciseCatalogModel


class MySQLExerciseRepository:
    """Read only reviewed catalog entries; public callers cannot create them."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, exercise_id: str) -> Exercise | None:
        async with self._sessions() as session:
            row = await session.get(ExerciseCatalogModel, exercise_id)
            return None if row is None else self._from_row(row)

    async def list_active(
        self,
        location: LocationType | None = None,
        equipment: set[str] | None = None,
    ) -> list[Exercise]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(ExerciseCatalogModel).order_by(ExerciseCatalogModel.id)
                )
            ).all()
        available = None if equipment is None else frozenset(equipment)
        values = [self._from_row(row) for row in rows]
        return [
            item
            for item in values
            if item.status is ExerciseStatus.ACTIVE
            and (location is None or location in item.location_types)
            and (available is None or item.required_equipment <= available)
        ]

    async def seed(self, exercises: Iterable[Exercise]) -> None:
        """Synchronize only the reviewed static seed; user data is never touched."""

        async with self._sessions() as session:
            async with session.begin():
                for exercise in exercises:
                    row = await session.get(ExerciseCatalogModel, exercise.id)
                    values = self._row_values(exercise)
                    if row is None:
                        session.add(ExerciseCatalogModel(id=exercise.id, **values))
                    else:
                        row.name = exercise.name
                        row.category = next(iter(sorted(exercise.movement_patterns)))
                        row.equipment = (
                            next(iter(sorted(exercise.required_equipment)))
                            if exercise.required_equipment
                            else "none"
                        )
                        row.payload = self._payload(exercise)
                        row.version = exercise.version

    @staticmethod
    def _row_values(exercise: Exercise) -> dict[str, object]:
        return {
            "name": exercise.name,
            "category": next(iter(sorted(exercise.movement_patterns))),
            "equipment": (
                next(iter(sorted(exercise.required_equipment)))
                if exercise.required_equipment
                else "none"
            ),
            "payload": MySQLExerciseRepository._payload(exercise),
            "version": exercise.version,
        }

    @staticmethod
    def _payload(exercise: Exercise) -> dict[str, object]:
        payload: dict[str, object] = {
            "difficulty_level": exercise.difficulty_level.value,
            "location_types": sorted(item.value for item in exercise.location_types),
            "required_equipment": sorted(exercise.required_equipment),
            "movement_patterns": sorted(exercise.movement_patterns),
            "feature_tags": sorted(exercise.feature_tags),
            "default_duration_seconds": exercise.default_duration_seconds,
            "status": exercise.status.value,
        }
        return payload

    @staticmethod
    def _from_row(row: ExerciseCatalogModel) -> Exercise:
        payload = row.payload
        return Exercise(
            id=row.id,
            name=row.name,
            difficulty_level=ExerciseDifficulty(str(payload["difficulty_level"])),
            location_types=frozenset(
                LocationType(item) for item in payload["location_types"]
            ),
            required_equipment=frozenset(payload["required_equipment"]),
            movement_patterns=frozenset(payload["movement_patterns"]),
            feature_tags=frozenset(payload["feature_tags"]),
            default_duration_seconds=int(payload["default_duration_seconds"]),
            status=ExerciseStatus(str(payload["status"])),
            version=row.version,
        )
