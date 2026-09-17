"""Review-only controlled Session Designer endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import get_current_user, get_session_design_service
from app.api.session_design_contracts import (
    CreateSessionDesignRequest,
    ReviewSessionDesignRequest,
    SessionDesignDraftResponse,
    SessionDesignMetricsResponse,
    SessionDesignTraceResponse,
)
from app.application.session_designs import SessionDesignService
from app.domain.session_design.models import SessionDesignRequest
from app.domain.users.models import UserAccount

router = APIRouter(tags=["session-designs"])


@router.post(
    "/session-designs",
    response_model=SessionDesignDraftResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session_design(
    payload: CreateSessionDesignRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[SessionDesignService, Depends(get_session_design_service)],
) -> SessionDesignDraftResponse:
    draft, reused = await service.create(
        user,
        SessionDesignRequest(
            client_request_id=payload.client_request_id,
            target_date=payload.target_date,
            target_duration_minutes=payload.target_duration_minutes,
            location=payload.location,
            goal=payload.goal,
            preferred_session_type=payload.preferred_session_type,
            template_id=payload.template_id,
        ),
    )
    if reused:
        response.status_code = status.HTTP_200_OK
    return SessionDesignDraftResponse.from_domain(draft)


@router.get("/session-designs/{draft_id}", response_model=SessionDesignDraftResponse)
async def get_session_design(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[SessionDesignService, Depends(get_session_design_service)],
) -> SessionDesignDraftResponse:
    return SessionDesignDraftResponse.from_domain(await service.get(user, draft_id))


@router.get(
    "/session-designs/{draft_id}/trace", response_model=SessionDesignTraceResponse
)
async def get_session_design_trace(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[SessionDesignService, Depends(get_session_design_service)],
) -> SessionDesignTraceResponse:
    return SessionDesignTraceResponse.from_domain(await service.trace(user, draft_id))


async def _review(
    *,
    accept: bool,
    draft_id: UUID,
    payload: ReviewSessionDesignRequest,
    user: UserAccount,
    service: SessionDesignService,
) -> SessionDesignDraftResponse:
    draft = await service.review(
        user,
        draft_id,
        expected_version=payload.expected_version,
        accept=accept,
    )
    return SessionDesignDraftResponse.from_domain(draft)


@router.post(
    "/session-designs/{draft_id}/accept", response_model=SessionDesignDraftResponse
)
async def accept_session_design(
    draft_id: UUID,
    payload: ReviewSessionDesignRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[SessionDesignService, Depends(get_session_design_service)],
) -> SessionDesignDraftResponse:
    return await _review(
        accept=True, draft_id=draft_id, payload=payload, user=user, service=service
    )


@router.post(
    "/session-designs/{draft_id}/reject", response_model=SessionDesignDraftResponse
)
async def reject_session_design(
    draft_id: UUID,
    payload: ReviewSessionDesignRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[SessionDesignService, Depends(get_session_design_service)],
) -> SessionDesignDraftResponse:
    return await _review(
        accept=False, draft_id=draft_id, payload=payload, user=user, service=service
    )


@router.get("/session-design/metrics", response_model=SessionDesignMetricsResponse)
async def get_session_design_metrics(
    service: Annotated[SessionDesignService, Depends(get_session_design_service)],
) -> SessionDesignMetricsResponse:
    return SessionDesignMetricsResponse(**service.metrics())
