"""Explicit preview/apply/query endpoints for Session Design Plan integration."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import (
    get_current_user,
    get_session_design_plan_application_service,
)
from app.api.session_design_application_contracts import (
    ApplySessionDesignRequest,
    SessionDesignApplyPreviewResponse,
    SessionDesignApplyResponse,
)
from app.application.session_design_application import (
    SessionDesignPlanApplicationService,
)
from app.domain.users.models import UserAccount

router = APIRouter(tags=["session-design-applications"])


@router.post(
    "/session-designs/{draft_id}/apply-preview",
    response_model=SessionDesignApplyPreviewResponse,
)
async def preview_session_design_application(
    draft_id: UUID,
    payload: ApplySessionDesignRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SessionDesignPlanApplicationService,
        Depends(get_session_design_plan_application_service),
    ],
) -> SessionDesignApplyPreviewResponse:
    preview = await service.preview(user, draft_id, payload.to_command())
    return SessionDesignApplyPreviewResponse.from_domain(preview)


@router.post(
    "/session-designs/{draft_id}/apply",
    response_model=SessionDesignApplyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def apply_session_design(
    draft_id: UUID,
    payload: ApplySessionDesignRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SessionDesignPlanApplicationService,
        Depends(get_session_design_plan_application_service),
    ],
) -> SessionDesignApplyResponse:
    outcome = await service.apply(user, draft_id, payload.to_command())
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return SessionDesignApplyResponse.from_outcome(outcome)


@router.get(
    "/session-designs/{draft_id}/application-result",
    response_model=SessionDesignApplyResponse,
)
async def get_session_design_application_result(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SessionDesignPlanApplicationService,
        Depends(get_session_design_plan_application_service),
    ],
) -> SessionDesignApplyResponse:
    return SessionDesignApplyResponse.from_outcome(
        await service.get_result(user, draft_id)
    )
