"""Read-only workout session endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.checkin_contracts import CheckInCreateRequest, CheckInResponse
from app.api.contracts import WorkoutSessionResponse
from app.api.dependencies import (
    get_check_in_service,
    get_current_user,
    get_session_service,
)
from app.application.checkins import CheckInService
from app.application.sessions import SessionService
from app.domain.users.models import UserAccount

router = APIRouter(tags=["sessions"])


@router.get(
    "/plans/{plan_id}/sessions",
    response_model=list[WorkoutSessionResponse],
)
async def list_plan_sessions(
    plan_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[SessionService, Depends(get_session_service)],
) -> list[WorkoutSessionResponse]:
    sessions = await service.list_plan_sessions(user, plan_id)
    return [WorkoutSessionResponse.from_domain(item) for item in sessions]


@router.get("/sessions/{session_id}", response_model=WorkoutSessionResponse)
async def get_session(
    session_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[SessionService, Depends(get_session_service)],
) -> WorkoutSessionResponse:
    return WorkoutSessionResponse.from_domain(
        await service.get_session(user, session_id)
    )


@router.post(
    "/sessions/{session_id}/check-ins",
    response_model=CheckInResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_check_in(
    session_id: UUID,
    payload: CheckInCreateRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[CheckInService, Depends(get_check_in_service)],
) -> CheckInResponse:
    result = await service.create(user, session_id, payload.to_command())
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return CheckInResponse.from_domain(result.check_in)


@router.get(
    "/sessions/{session_id}/check-in",
    response_model=CheckInResponse,
)
async def get_check_in(
    session_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[CheckInService, Depends(get_check_in_service)],
) -> CheckInResponse:
    return CheckInResponse.from_domain(await service.get_for_session(user, session_id))
