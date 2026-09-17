"""Structured weekly plan endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.checkin_contracts import PlanProgressResponse
from app.api.contracts import (
    GenerateWeeklyPlanRequest,
    PlanConfirmRequest,
    PlanCreateRequest,
    PlanCreationResponse,
    PlanGenerationResponse,
    WeeklyPlanResponse,
)
from app.api.dependencies import (
    get_current_user,
    get_local_replanning_service,
    get_plan_generation_service,
    get_plan_service,
    get_progress_service,
)
from app.api.replanning_contracts import (
    LocalReplanRequest,
    LocalReplanResponse,
    PlanRevisionResponse,
)
from app.application.local_replanning import LocalReplanningService
from app.application.plan_generation import PlanGenerationService
from app.application.plans import (
    CreatePlanCommand,
    PlanService,
    SessionExerciseCommand,
    WorkoutSessionCommand,
)
from app.application.progress import ProgressService
from app.domain.users.models import UserAccount

router = APIRouter(prefix="/plans", tags=["plans"])


@router.post(
    "",
    response_model=PlanCreationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_plan(
    payload: PlanCreateRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[PlanService, Depends(get_plan_service)],
) -> PlanCreationResponse:
    result = await service.create_plan(
        user,
        CreatePlanCommand(
            week_start=payload.week_start,
            revision=payload.revision,
            sessions=tuple(
                WorkoutSessionCommand(
                    scheduled_start=session.scheduled_start,
                    scheduled_end=session.scheduled_end,
                    location_type=session.location_type,
                    session_type=session.session_type,
                    estimated_minutes=session.estimated_minutes,
                    target_difficulty=session.target_difficulty,
                    exercises=tuple(
                        SessionExerciseCommand(
                            exercise_id=exercise.exercise_id,
                            sequence_no=exercise.sequence_no,
                            sets=exercise.sets,
                            repetitions=exercise.repetitions,
                            duration_seconds=exercise.duration_seconds,
                            rest_seconds=exercise.rest_seconds,
                        )
                        for exercise in session.exercises
                    ),
                )
                for session in payload.sessions
            ),
        ),
    )
    return PlanCreationResponse.from_result(result)


@router.post(
    "/generate",
    response_model=PlanGenerationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_plan(
    payload: GenerateWeeklyPlanRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        PlanGenerationService,
        Depends(get_plan_generation_service),
    ],
) -> PlanGenerationResponse:
    """Generate a safe plan from structured availability without an LLM."""

    result = await service.generate_plan(user, payload.to_command())
    return PlanGenerationResponse.from_result(result)


@router.get("", response_model=list[WeeklyPlanResponse])
async def list_plans(
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[PlanService, Depends(get_plan_service)],
) -> list[WeeklyPlanResponse]:
    return [
        WeeklyPlanResponse.from_domain(item) for item in await service.list_plans(user)
    ]


@router.get("/{plan_id}", response_model=WeeklyPlanResponse)
async def get_plan(
    plan_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[PlanService, Depends(get_plan_service)],
) -> WeeklyPlanResponse:
    return WeeklyPlanResponse.from_domain(await service.get_plan(user, plan_id))


@router.post("/{plan_id}/confirm", response_model=WeeklyPlanResponse)
async def confirm_plan(
    plan_id: UUID,
    payload: PlanConfirmRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[PlanService, Depends(get_plan_service)],
) -> WeeklyPlanResponse:
    plan = await service.confirm_plan(
        user,
        plan_id,
        expected_version=payload.expected_version,
    )
    return WeeklyPlanResponse.from_domain(plan)


@router.get("/{plan_id}/progress", response_model=PlanProgressResponse)
async def get_plan_progress(
    plan_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ProgressService, Depends(get_progress_service)],
) -> PlanProgressResponse:
    return PlanProgressResponse.from_domain(await service.summarize(user, plan_id))


@router.post(
    "/{plan_id}/replan",
    response_model=LocalReplanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def replan(
    plan_id: UUID,
    payload: LocalReplanRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[LocalReplanningService, Depends(get_local_replanning_service)],
) -> LocalReplanResponse:
    result = await service.replan(user, plan_id, payload.to_command())
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return LocalReplanResponse(
        plan=WeeklyPlanResponse.from_domain(result.plan),
        created=result.created,
        repair_attempts=result.repair_attempts,
    )


@router.get(
    "/{plan_id}/revisions",
    response_model=list[PlanRevisionResponse],
)
async def list_revisions(
    plan_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[LocalReplanningService, Depends(get_local_replanning_service)],
) -> list[PlanRevisionResponse]:
    revisions = await service.list_revisions(user, plan_id)
    return [
        PlanRevisionResponse.create(
            WeeklyPlanResponse.from_domain(item),
            await service.is_current_revision(user, item),
        )
        for item in revisions
    ]


@router.get(
    "/{plan_id}/revisions/{revision}",
    response_model=PlanRevisionResponse,
)
async def get_revision(
    plan_id: UUID,
    revision: int,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[LocalReplanningService, Depends(get_local_replanning_service)],
) -> PlanRevisionResponse:
    plan = await service.get_revision(user, plan_id, revision)
    return PlanRevisionResponse.create(
        WeeklyPlanResponse.from_domain(plan),
        await service.is_current_revision(user, plan),
    )


@router.post(
    "/{plan_id}/revisions/{revision}/confirm",
    response_model=PlanRevisionResponse,
)
async def confirm_revision(
    plan_id: UUID,
    revision: int,
    payload: PlanConfirmRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[LocalReplanningService, Depends(get_local_replanning_service)],
) -> PlanRevisionResponse:
    plan = await service.confirm_revision(
        user,
        plan_id,
        revision,
        expected_version=payload.expected_version,
    )
    return PlanRevisionResponse.create(
        WeeklyPlanResponse.from_domain(plan),
        await service.is_current_revision(user, plan),
    )
