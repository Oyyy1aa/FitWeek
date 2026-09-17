"""Explicit Schedule Draft preview/apply/result endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import (
    get_current_user,
    get_schedule_plan_application_service,
)
from app.api.schedule_application_contracts import (
    ApplyScheduleDraftRequest,
    ScheduleApplyPreviewResponse,
    ScheduleApplyResponse,
)
from app.application.schedule_application import SchedulePlanApplicationService
from app.domain.users.models import UserAccount

router = APIRouter(tags=["schedule-applications"])


@router.post(
    "/schedule-drafts/{draft_id}/apply-preview",
    response_model=ScheduleApplyPreviewResponse,
)
async def preview_schedule_application(
    draft_id: UUID,
    payload: ApplyScheduleDraftRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SchedulePlanApplicationService,
        Depends(get_schedule_plan_application_service),
    ],
) -> ScheduleApplyPreviewResponse:
    return ScheduleApplyPreviewResponse.from_domain(
        await service.preview(user, draft_id, payload.to_command())
    )


@router.post(
    "/schedule-drafts/{draft_id}/apply",
    response_model=ScheduleApplyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def apply_schedule_draft(
    draft_id: UUID,
    payload: ApplyScheduleDraftRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SchedulePlanApplicationService,
        Depends(get_schedule_plan_application_service),
    ],
) -> ScheduleApplyResponse:
    outcome = await service.apply(user, draft_id, payload.to_command())
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return ScheduleApplyResponse.from_outcome(outcome)


@router.get(
    "/schedule-drafts/{draft_id}/application-result",
    response_model=ScheduleApplyResponse,
)
async def get_schedule_application_result(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SchedulePlanApplicationService,
        Depends(get_schedule_plan_application_service),
    ],
) -> ScheduleApplyResponse:
    return ScheduleApplyResponse.from_outcome(await service.get_result(user, draft_id))
