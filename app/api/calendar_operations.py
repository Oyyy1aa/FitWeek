"""Explicit review and execution API for Calendar operation drafts."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.calendar_operation_contracts import (
    CalendarBindingResponse,
    CalendarOperationAttemptResponse,
    CalendarOperationDraftResponse,
    CreateCalendarOperationRequest,
    ReviewCalendarOperationRequest,
)
from app.api.dependencies import get_calendar_operation_service, get_current_user
from app.application.calendar_operations import CalendarOperationService
from app.domain.users.models import UserAccount

router = APIRouter(tags=["calendar-operations"])


@router.post(
    "/plans/{root_plan_id}/revisions/{revision}/calendar-operation-drafts",
    response_model=CalendarOperationDraftResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_calendar_operation_draft(
    root_plan_id: UUID,
    revision: int,
    payload: CreateCalendarOperationRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationService, Depends(get_calendar_operation_service)
    ],
) -> CalendarOperationDraftResponse:
    draft, created = await service.create_draft(
        user, root_plan_id, revision, payload.to_command()
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return CalendarOperationDraftResponse.from_domain(draft)


@router.get(
    "/calendar-operation-drafts/{draft_id}",
    response_model=CalendarOperationDraftResponse,
)
async def get_calendar_operation_draft(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationService, Depends(get_calendar_operation_service)
    ],
) -> CalendarOperationDraftResponse:
    return CalendarOperationDraftResponse.from_domain(
        await service.get_draft(user, draft_id)
    )


@router.post(
    "/calendar-operation-drafts/{draft_id}/approve",
    response_model=CalendarOperationDraftResponse,
)
async def approve_calendar_operation_draft(
    draft_id: UUID,
    payload: ReviewCalendarOperationRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationService, Depends(get_calendar_operation_service)
    ],
) -> CalendarOperationDraftResponse:
    return CalendarOperationDraftResponse.from_domain(
        await service.approve(user, draft_id, payload.expected_version)
    )


@router.post(
    "/calendar-operation-drafts/{draft_id}/reject",
    response_model=CalendarOperationDraftResponse,
)
async def reject_calendar_operation_draft(
    draft_id: UUID,
    payload: ReviewCalendarOperationRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationService, Depends(get_calendar_operation_service)
    ],
) -> CalendarOperationDraftResponse:
    return CalendarOperationDraftResponse.from_domain(
        await service.reject(user, draft_id, payload.expected_version)
    )


@router.post(
    "/calendar-operation-drafts/{draft_id}/execute",
    response_model=CalendarOperationDraftResponse,
)
@router.post(
    "/calendar-operation-drafts/{draft_id}/retry",
    response_model=CalendarOperationDraftResponse,
)
async def execute_calendar_operation_draft(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
) -> CalendarOperationDraftResponse:
    del draft_id, user
    from fastapi import HTTPException

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail="Use Calendar Run control plane."
    )


@router.get(
    "/calendar-operation-drafts/{draft_id}/attempts",
    response_model=tuple[CalendarOperationAttemptResponse, ...],
)
async def list_calendar_operation_attempts(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationService, Depends(get_calendar_operation_service)
    ],
) -> tuple[CalendarOperationAttemptResponse, ...]:
    return tuple(
        CalendarOperationAttemptResponse.from_domain(item)
        for item in await service.attempts(user, draft_id)
    )


@router.get("/calendar-bindings", response_model=tuple[CalendarBindingResponse, ...])
async def list_calendar_bindings(
    provider: str,
    calendar_id: str,
    root_plan_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        CalendarOperationService, Depends(get_calendar_operation_service)
    ],
) -> tuple[CalendarBindingResponse, ...]:
    return tuple(
        CalendarBindingResponse.from_domain(item)
        for item in await service.bindings(
            user,
            provider=provider,
            calendar_id=calendar_id,
            root_plan_id=root_plan_id,
        )
    )
