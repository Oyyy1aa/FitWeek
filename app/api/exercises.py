"""Read-only controlled exercise catalog endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.contracts import ExerciseResponse
from app.api.dependencies import get_exercise_service
from app.application.exercises import ExerciseCatalogService
from app.domain.common import LocationType

router = APIRouter(prefix="/exercises", tags=["exercises"])


@router.get("", response_model=list[ExerciseResponse])
async def list_exercises(
    service: Annotated[ExerciseCatalogService, Depends(get_exercise_service)],
    location: LocationType | None = None,
    equipment: Annotated[list[str] | None, Query()] = None,
) -> list[ExerciseResponse]:
    exercises = await service.list_exercises(
        location=location,
        equipment=None if equipment is None else set(equipment),
    )
    return [ExerciseResponse.from_domain(item) for item in exercises]


@router.get("/{exercise_id}", response_model=ExerciseResponse)
async def get_exercise(
    exercise_id: str,
    service: Annotated[ExerciseCatalogService, Depends(get_exercise_service)],
) -> ExerciseResponse:
    return ExerciseResponse.from_domain(await service.get_exercise(exercise_id))
