"""Phase 3A draft-only Profile Agent endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import (
    get_current_user,
    get_profile_agent_service,
    get_profile_draft_apply_service,
    get_profile_draft_memory_candidate_service,
    get_profile_draft_preview_service,
)
from app.api.profile_agent_contracts import (
    DraftMemoryCandidateImportResponse,
    DraftMemoryCandidatePreviewResponse,
    ImportDraftMemoryCandidatesRequest,
    ModelCallTraceResponse,
    ModelGatewayMetricsResponse,
    ParseProfileRequest,
    PreviewDraftMemoryCandidatesRequest,
    ProfileAgentDraftResponse,
    ProfileDraftApplyDecisionRequest,
    ProfileDraftApplyPreviewResponse,
    ProfileDraftApplyResultResponse,
    ProfileDraftRejectResponse,
    RejectProfileDraftRequest,
)
from app.application.profile_agent import ParseProfileCommand, ProfileAgentService
from app.domain.profile_agent.memory_candidates import (
    ImportDraftMemoryCandidatesCommand,
)
from app.domain.users.models import UserAccount
from app.profile_application.apply import ProfileDraftApplyService
from app.profile_application.memory_candidates import ProfileDraftMemoryCandidateService
from app.profile_application.preview import ProfileDraftPreviewService

router = APIRouter(tags=["profile-agent"])


@router.post(
    "/profile-agent/parse",
    response_model=ProfileAgentDraftResponse,
    status_code=status.HTTP_201_CREATED,
)
async def parse_profile_request(
    payload: ParseProfileRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ProfileAgentService, Depends(get_profile_agent_service)],
) -> ProfileAgentDraftResponse:
    result = await service.parse(
        user,
        ParseProfileCommand(
            client_request_id=payload.client_request_id,
            user_message=payload.user_message,
            current_week=payload.current_week,
        ),
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return ProfileAgentDraftResponse.from_domain(result.draft)


@router.get(
    "/profile-agent/drafts",
    response_model=list[ProfileAgentDraftResponse],
)
async def list_profile_agent_drafts(
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ProfileAgentService, Depends(get_profile_agent_service)],
) -> list[ProfileAgentDraftResponse]:
    return [
        ProfileAgentDraftResponse.from_domain(draft)
        for draft in await service.list_drafts(user)
    ]


@router.get(
    "/profile-agent/drafts/{draft_id}",
    response_model=ProfileAgentDraftResponse,
)
async def get_profile_agent_draft(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ProfileAgentService, Depends(get_profile_agent_service)],
) -> ProfileAgentDraftResponse:
    return ProfileAgentDraftResponse.from_domain(
        await service.get_draft(user, draft_id)
    )


@router.post(
    "/profile-agent/drafts/{draft_id}/apply-preview",
    response_model=ProfileDraftApplyPreviewResponse,
)
async def preview_profile_agent_draft(
    draft_id: UUID,
    payload: ProfileDraftApplyDecisionRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileDraftPreviewService,
        Depends(get_profile_draft_preview_service),
    ],
) -> ProfileDraftApplyPreviewResponse:
    preview = await service.preview(
        user=user,
        draft_id=draft_id,
        decision=payload.to_domain(),
    )
    return ProfileDraftApplyPreviewResponse.from_domain(preview)


@router.post(
    "/profile-agent/drafts/{draft_id}/apply",
    response_model=ProfileDraftApplyResultResponse,
)
async def apply_profile_agent_draft(
    draft_id: UUID,
    payload: ProfileDraftApplyDecisionRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileDraftApplyService,
        Depends(get_profile_draft_apply_service),
    ],
) -> ProfileDraftApplyResultResponse:
    committed = await service.apply(
        user=user,
        draft_id=draft_id,
        decision=payload.to_domain(),
    )
    return ProfileDraftApplyResultResponse.from_domain(committed.result)


@router.post(
    "/profile-agent/drafts/{draft_id}/reject",
    response_model=ProfileDraftRejectResponse,
)
async def reject_profile_agent_draft(
    draft_id: UUID,
    payload: RejectProfileDraftRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileDraftApplyService,
        Depends(get_profile_draft_apply_service),
    ],
) -> ProfileDraftRejectResponse:
    result = await service.reject(
        user=user,
        draft_id=draft_id,
        client_request_id=payload.client_request_id,
        expected_draft_version=payload.expected_draft_version,
    )
    return ProfileDraftRejectResponse.from_domain(result)


@router.get(
    "/profile-agent/drafts/{draft_id}/apply-result",
    response_model=ProfileDraftApplyResultResponse,
)
async def get_profile_agent_apply_result(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileDraftApplyService,
        Depends(get_profile_draft_apply_service),
    ],
) -> ProfileDraftApplyResultResponse:
    return ProfileDraftApplyResultResponse.from_domain(
        await service.get_apply_result(user=user, draft_id=draft_id)
    )


@router.post(
    "/profile-agent/drafts/{draft_id}/memory-candidates/preview",
    response_model=DraftMemoryCandidatePreviewResponse,
)
async def preview_draft_memory_candidates(
    draft_id: UUID,
    payload: PreviewDraftMemoryCandidatesRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileDraftMemoryCandidateService,
        Depends(get_profile_draft_memory_candidate_service),
    ],
) -> DraftMemoryCandidatePreviewResponse:
    return DraftMemoryCandidatePreviewResponse.from_domain(
        await service.preview(
            user=user,
            draft_id=draft_id,
            selected_candidate_indexes=payload.selected_candidate_indexes,
        )
    )


@router.post(
    "/profile-agent/drafts/{draft_id}/memory-candidates",
    response_model=DraftMemoryCandidateImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_draft_memory_candidates(
    draft_id: UUID,
    payload: ImportDraftMemoryCandidatesRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileDraftMemoryCandidateService,
        Depends(get_profile_draft_memory_candidate_service),
    ],
) -> DraftMemoryCandidateImportResponse:
    result = await service.import_candidates(
        user=user,
        draft_id=draft_id,
        command=ImportDraftMemoryCandidatesCommand(
            client_request_id=payload.client_request_id,
            expected_draft_version=payload.expected_draft_version,
            selected_candidate_indexes=payload.selected_candidate_indexes,
        ),
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return DraftMemoryCandidateImportResponse.from_domain(result)


@router.get(
    "/profile-agent/requests/{request_id}/traces",
    response_model=list[ModelCallTraceResponse],
)
async def list_profile_agent_traces(
    request_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ProfileAgentService, Depends(get_profile_agent_service)],
) -> list[ModelCallTraceResponse]:
    return [
        ModelCallTraceResponse.from_domain(trace)
        for trace in await service.list_traces(user, request_id)
    ]


@router.get(
    "/model-gateway/metrics",
    response_model=ModelGatewayMetricsResponse,
)
async def get_model_gateway_metrics(
    service: Annotated[ProfileAgentService, Depends(get_profile_agent_service)],
) -> ModelGatewayMetricsResponse:
    return ModelGatewayMetricsResponse.from_domain(service.metrics())
