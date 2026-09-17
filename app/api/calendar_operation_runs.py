"""Control-plane API for approved Calendar operation Runs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import get_calendar_operation_run_service, get_current_user
from app.api.orchestration_contracts import (
    AgentStepResponse,
    AuditEventResponse,
    PlanningRunResponse,
    StepCheckpointResponse,
)
from app.application.calendar_operation_orchestration import (
    CalendarOperationRunService,
)
from app.domain.users.models import UserAccount

router = APIRouter(tags=["calendar-operation-runs"])


class CreateCalendarOperationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=128)
    draft_id: UUID


@router.post(
    "/calendar-operation-runs",
    response_model=PlanningRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_calendar_operation_run(
    payload: CreateCalendarOperationRunRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationRunService,
        Depends(get_calendar_operation_run_service),
    ],
) -> PlanningRunResponse:
    result = await service.create_run(user, **payload.model_dump())
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return PlanningRunResponse.from_domain(result.run)


@router.get("/calendar-operation-runs/{run_id}", response_model=PlanningRunResponse)
async def get_calendar_operation_run(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationRunService,
        Depends(get_calendar_operation_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(await service.get_run(user, run_id))


@router.get(
    "/calendar-operation-runs/{run_id}/steps",
    response_model=list[AgentStepResponse],
)
async def list_calendar_operation_run_steps(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationRunService,
        Depends(get_calendar_operation_run_service),
    ],
) -> list[AgentStepResponse]:
    return [
        AgentStepResponse.from_domain(item)
        for item in await service.list_steps(user, run_id)
    ]


@router.get(
    "/calendar-operation-runs/{run_id}/checkpoints",
    response_model=list[StepCheckpointResponse],
)
async def list_calendar_operation_run_checkpoints(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationRunService,
        Depends(get_calendar_operation_run_service),
    ],
) -> list[StepCheckpointResponse]:
    return [
        StepCheckpointResponse.from_domain(item)
        for item in await service.list_checkpoints(user, run_id)
    ]


@router.get(
    "/calendar-operation-runs/{run_id}/audit",
    response_model=list[AuditEventResponse],
)
async def list_calendar_operation_run_audit(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationRunService,
        Depends(get_calendar_operation_run_service),
    ],
) -> list[AuditEventResponse]:
    return [
        AuditEventResponse.from_domain(item)
        for item in await service.list_audit(user, run_id)
    ]


@router.post(
    "/calendar-operation-runs/{run_id}/retry",
    response_model=PlanningRunResponse,
)
async def retry_calendar_operation_run(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationRunService,
        Depends(get_calendar_operation_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(await service.retry_run(user, run_id))


@router.post(
    "/calendar-operation-runs/{run_id}/cancel",
    response_model=PlanningRunResponse,
)
async def cancel_calendar_operation_run(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationRunService,
        Depends(get_calendar_operation_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(await service.cancel_run(user, run_id))
