"""Phase 4A Memory CRUD and development-only Candidate review endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import get_current_user, get_memory_application_service
from app.api.memory_contracts import (
    AcceptMemoryCandidateRequest,
    CandidateReviewResponse,
    CreateMemoryCandidateRequest,
    CreateMemoryRequest,
    DeleteMemoryRequest,
    MemoryCandidateResponse,
    MemoryMetricsResponse,
    MemoryResponse,
    RejectMemoryCandidateRequest,
    ReplaceMemoryRequest,
    ReplaceMemoryResponse,
    UpdateMemoryRequest,
)
from app.application.memories import MemoryApplicationService
from app.domain.users.models import UserAccount
from app.memory.candidate_service import (
    AcceptCandidateCommand,
    CreateCandidateCommand,
    RejectCandidateCommand,
)
from app.memory.service import (
    CreateMemoryCommand,
    ReplaceMemoryCommand,
    UpdateMemoryCommand,
)

router = APIRouter(tags=["memory"])


@router.post(
    "/memories", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED
)
async def create_memory(
    payload: CreateMemoryRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> MemoryResponse:
    outcome = await service.create_memory(
        user,
        CreateMemoryCommand(
            client_request_id=payload.client_request_id,
            memory_type=payload.memory_type,
            key=payload.key,
            value=payload.value,
            valid_until=payload.valid_until,
        ),
    )
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return MemoryResponse.from_domain(outcome.record)


@router.get("/memories", response_model=list[MemoryResponse])
async def list_memories(
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> list[MemoryResponse]:
    return [
        MemoryResponse.from_domain(item) for item in await service.list_memories(user)
    ]


@router.get("/memories/{memory_id}", response_model=MemoryResponse)
async def get_memory(
    memory_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> MemoryResponse:
    return MemoryResponse.from_domain(await service.get_memory(user, memory_id))


@router.put("/memories/{memory_id}", response_model=MemoryResponse)
async def update_memory(
    memory_id: UUID,
    payload: UpdateMemoryRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> MemoryResponse:
    return MemoryResponse.from_domain(
        await service.update_memory(
            user,
            memory_id,
            UpdateMemoryCommand(
                expected_version=payload.expected_version,
                value=payload.value,
                valid_until=payload.valid_until,
            ),
        )
    )


@router.delete("/memories/{memory_id}", response_model=MemoryResponse)
async def delete_memory(
    memory_id: UUID,
    payload: DeleteMemoryRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> MemoryResponse:
    return MemoryResponse.from_domain(
        await service.delete_memory(user, memory_id, payload.expected_version)
    )


@router.post("/memories/{memory_id}/replace", response_model=ReplaceMemoryResponse)
async def replace_memory(
    memory_id: UUID,
    payload: ReplaceMemoryRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> ReplaceMemoryResponse:
    return ReplaceMemoryResponse.from_domain(
        await service.replace_memory(
            user,
            memory_id,
            ReplaceMemoryCommand(
                client_request_id=payload.client_request_id,
                expected_version=payload.expected_version,
                value=payload.value,
                valid_until=payload.valid_until,
            ),
        )
    )


@router.post(
    "/memory-candidates",
    response_model=MemoryCandidateResponse,
    status_code=status.HTTP_201_CREATED,
    description="Development-only structured Candidate creation endpoint.",
)
async def create_memory_candidate(
    payload: CreateMemoryCandidateRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> MemoryCandidateResponse:
    outcome = await service.create_candidate(
        user,
        CreateCandidateCommand(
            client_request_id=payload.client_request_id,
            memory_type=payload.memory_type,
            key=payload.key,
            value=payload.value,
            source=payload.source,
            source_reference=payload.source_reference,
            evidence_summary=payload.evidence_summary,
            confidence=payload.confidence,
            expires_at=payload.expires_at,
        ),
    )
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return MemoryCandidateResponse.from_domain(outcome.candidate)


@router.get("/memory-candidates", response_model=list[MemoryCandidateResponse])
async def list_memory_candidates(
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> list[MemoryCandidateResponse]:
    return [
        MemoryCandidateResponse.from_domain(item)
        for item in await service.list_candidates(user)
    ]


@router.get("/memory-candidates/{candidate_id}", response_model=MemoryCandidateResponse)
async def get_memory_candidate(
    candidate_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> MemoryCandidateResponse:
    return MemoryCandidateResponse.from_domain(
        await service.get_candidate(user, candidate_id)
    )


@router.post(
    "/memory-candidates/{candidate_id}/accept", response_model=CandidateReviewResponse
)
async def accept_memory_candidate(
    candidate_id: UUID,
    payload: AcceptMemoryCandidateRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> CandidateReviewResponse:
    return CandidateReviewResponse.from_domain(
        await service.accept_candidate(
            user,
            candidate_id,
            AcceptCandidateCommand(
                client_request_id=payload.client_request_id,
                expected_candidate_version=payload.expected_candidate_version,
                confirmed_value=payload.confirmed_value,
                valid_until=payload.valid_until,
            ),
        )
    )


@router.post(
    "/memory-candidates/{candidate_id}/reject", response_model=CandidateReviewResponse
)
async def reject_memory_candidate(
    candidate_id: UUID,
    payload: RejectMemoryCandidateRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> CandidateReviewResponse:
    return CandidateReviewResponse.from_domain(
        await service.reject_candidate(
            user,
            candidate_id,
            RejectCandidateCommand(
                client_request_id=payload.client_request_id,
                expected_candidate_version=payload.expected_candidate_version,
            ),
        )
    )


@router.get("/memory/metrics", response_model=MemoryMetricsResponse)
async def get_memory_metrics(
    service: Annotated[
        MemoryApplicationService, Depends(get_memory_application_service)
    ],
) -> MemoryMetricsResponse:
    return MemoryMetricsResponse(**service.metrics())
