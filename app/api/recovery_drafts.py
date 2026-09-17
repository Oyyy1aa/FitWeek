"""Phase 7A Recovery Draft review API; no Plan, Memory, or Calendar writes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.calendar_operation_contracts import (
    CalendarOperationDraftResponse,
    CreateCalendarOperationRequest,
)
from app.api.contracts import WeeklyPlanResponse
from app.api.dependencies import (
    get_current_user,
    get_recovery_application_service,
    get_recovery_draft_service,
)
from app.api.recovery_contracts import (
    ApplyRecoveryDraftRequest,
    BehaviorMemoryProposalResponse,
    BehaviorSummaryResponse,
    CreateRecoveryDraftRequest,
    ImportRecoveryMemoryProposalsRequest,
    RecoveryApplicationResultResponse,
    RecoveryApplyPreviewResponse,
    RecoveryApplyResponse,
    RecoveryCandidateSetResponse,
    RecoveryChangeImpactResponse,
    RecoveryDraftResponse,
    RecoveryMemoryProposalImportResponse,
    RecoveryMemoryProposalPreviewResponse,
    RecoveryMemoryProposalSelectionRequest,
    RecoveryMetricsResponse,
    RecoverySubdraftsResponse,
    RecoveryTraceResponse,
    ReviewRecoveryDraftRequest,
)
from app.application.recovery_applications import RecoveryApplicationService
from app.application.recovery_drafts import RecoveryDraftService
from app.domain.users.models import UserAccount

router = APIRouter(tags=["recovery"])


@router.post("/recovery-drafts", response_model=RecoveryDraftResponse)
async def create_recovery_draft(
    payload: CreateRecoveryDraftRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[RecoveryDraftService, Depends(get_recovery_draft_service)],
) -> RecoveryDraftResponse:
    result = await service.create(user, payload.to_domain())
    response.status_code = (
        status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    )
    return RecoveryDraftResponse.from_domain(result.draft)


@router.get("/recovery-drafts/{draft_id}", response_model=RecoveryDraftResponse)
async def get_recovery_draft(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[RecoveryDraftService, Depends(get_recovery_draft_service)],
) -> RecoveryDraftResponse:
    return RecoveryDraftResponse.from_domain(await service.get(user, draft_id))


@router.get(
    "/recovery-drafts/{draft_id}/behavior-summary",
    response_model=BehaviorSummaryResponse,
)
async def get_behavior_summary(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[RecoveryDraftService, Depends(get_recovery_draft_service)],
) -> BehaviorSummaryResponse:
    return BehaviorSummaryResponse.from_domain(
        await service.get_summary(user, draft_id)
    )


@router.get(
    "/recovery-drafts/{draft_id}/change-impact",
    response_model=RecoveryChangeImpactResponse,
)
async def get_change_impact(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[RecoveryDraftService, Depends(get_recovery_draft_service)],
) -> RecoveryChangeImpactResponse:
    return RecoveryChangeImpactResponse.from_domain(
        await service.get_impact(user, draft_id)
    )


@router.get(
    "/recovery-drafts/{draft_id}/candidate-set",
    response_model=RecoveryCandidateSetResponse,
)
async def get_candidate_set(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[RecoveryDraftService, Depends(get_recovery_draft_service)],
) -> RecoveryCandidateSetResponse:
    return RecoveryCandidateSetResponse.from_domain(
        await service.get_candidate_set(user, draft_id)
    )


@router.get(
    "/recovery-drafts/{draft_id}/memory-proposals",
    response_model=tuple[BehaviorMemoryProposalResponse, ...],
)
async def list_memory_proposals(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[RecoveryDraftService, Depends(get_recovery_draft_service)],
) -> tuple[BehaviorMemoryProposalResponse, ...]:
    return tuple(
        BehaviorMemoryProposalResponse.from_domain(item)
        for item in await service.list_proposals(user, draft_id)
    )


@router.get("/recovery-drafts/{draft_id}/trace", response_model=RecoveryTraceResponse)
async def get_recovery_trace(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[RecoveryDraftService, Depends(get_recovery_draft_service)],
) -> RecoveryTraceResponse:
    return RecoveryTraceResponse.from_domain(await service.get_trace(user, draft_id))


@router.post("/recovery-drafts/{draft_id}/accept", response_model=RecoveryDraftResponse)
async def accept_recovery_draft(
    draft_id: UUID,
    payload: ReviewRecoveryDraftRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[RecoveryDraftService, Depends(get_recovery_draft_service)],
) -> RecoveryDraftResponse:
    return RecoveryDraftResponse.from_domain(
        await service.accept(user, draft_id, expected_version=payload.expected_version)
    )


@router.post("/recovery-drafts/{draft_id}/reject", response_model=RecoveryDraftResponse)
async def reject_recovery_draft(
    draft_id: UUID,
    payload: ReviewRecoveryDraftRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[RecoveryDraftService, Depends(get_recovery_draft_service)],
) -> RecoveryDraftResponse:
    return RecoveryDraftResponse.from_domain(
        await service.reject(user, draft_id, expected_version=payload.expected_version)
    )


@router.get("/recovery/metrics", response_model=RecoveryMetricsResponse)
async def get_recovery_metrics(
    service: Annotated[RecoveryDraftService, Depends(get_recovery_draft_service)],
) -> RecoveryMetricsResponse:
    return RecoveryMetricsResponse(**service.metrics())


@router.post(
    "/recovery-drafts/{draft_id}/apply-preview",
    response_model=RecoveryApplyPreviewResponse,
)
async def preview_recovery_application(
    draft_id: UUID,
    payload: ApplyRecoveryDraftRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationService, Depends(get_recovery_application_service)
    ],
) -> RecoveryApplyPreviewResponse:
    return RecoveryApplyPreviewResponse.from_domain(
        await service.preview(user, draft_id, payload.to_domain())
    )


@router.post(
    "/recovery-drafts/{draft_id}/subdrafts",
    response_model=RecoverySubdraftsResponse,
)
async def create_recovery_subdrafts(
    draft_id: UUID,
    payload: ApplyRecoveryDraftRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationService, Depends(get_recovery_application_service)
    ],
) -> RecoverySubdraftsResponse:
    result = await service.create_subdrafts(user, draft_id, payload.to_domain())
    return RecoverySubdraftsResponse(
        session_design_draft_ids=result.session_design_draft_ids,
        schedule_draft_ids=result.schedule_draft_ids,
        created=result.created,
    )


@router.post(
    "/recovery-drafts/{draft_id}/apply",
    response_model=RecoveryApplyResponse,
)
async def apply_recovery_draft(
    draft_id: UUID,
    payload: ApplyRecoveryDraftRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationService, Depends(get_recovery_application_service)
    ],
) -> RecoveryApplyResponse:
    result = await service.apply(user, draft_id, payload.to_domain())
    response.status_code = (
        status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    )
    return RecoveryApplyResponse(
        result=RecoveryApplicationResultResponse.from_domain(result.result),
        draft=RecoveryDraftResponse.from_domain(result.draft),
        plan=WeeklyPlanResponse.from_domain(result.plan) if result.plan else None,
        created=result.created,
    )


@router.get(
    "/recovery-drafts/{draft_id}/application-result",
    response_model=RecoveryApplyResponse,
)
async def get_recovery_application_result(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationService, Depends(get_recovery_application_service)
    ],
) -> RecoveryApplyResponse:
    result = await service.get_result(user, draft_id)
    return RecoveryApplyResponse(
        result=RecoveryApplicationResultResponse.from_domain(result.result),
        draft=RecoveryDraftResponse.from_domain(result.draft),
        plan=WeeklyPlanResponse.from_domain(result.plan) if result.plan else None,
        created=False,
    )


@router.post(
    "/recovery-drafts/{draft_id}/memory-proposals/preview",
    response_model=RecoveryMemoryProposalPreviewResponse,
)
async def preview_recovery_memory_proposals(
    draft_id: UUID,
    payload: RecoveryMemoryProposalSelectionRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationService, Depends(get_recovery_application_service)
    ],
) -> RecoveryMemoryProposalPreviewResponse:
    return RecoveryMemoryProposalPreviewResponse.from_domain(
        await service.memory_preview(user, draft_id, payload.selected_proposal_ids)
    )


@router.post(
    "/recovery-drafts/{draft_id}/memory-proposals/import",
    response_model=RecoveryMemoryProposalImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_recovery_memory_proposals(
    draft_id: UUID,
    payload: ImportRecoveryMemoryProposalsRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationService, Depends(get_recovery_application_service)
    ],
) -> RecoveryMemoryProposalImportResponse:
    outcome = await service.import_memory_proposals(user, draft_id, payload.to_domain())
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return RecoveryMemoryProposalImportResponse.from_domain(outcome.result)


@router.post(
    "/recovery-application-results/{result_id}/calendar-operation-draft",
    response_model=CalendarOperationDraftResponse,
)
async def create_recovery_calendar_operation_draft(
    result_id: UUID,
    payload: CreateCalendarOperationRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationService, Depends(get_recovery_application_service)
    ],
) -> CalendarOperationDraftResponse:
    result, created = await service.create_calendar_operation_draft(
        user, result_id, payload.to_command()
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return CalendarOperationDraftResponse.from_domain(result)
