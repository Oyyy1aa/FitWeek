"""Phase 3B Profile Agent review workflow control-plane endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import get_current_user, get_profile_agent_run_service
from app.api.orchestration_contracts import (
    AgentStepResponse,
    AuditEventResponse,
    PlanningRunResponse,
    StepCheckpointResponse,
)
from app.api.profile_agent_contracts import (
    CreateProfileAgentRunRequest,
    ProfileAgentRunCreateResponse,
    ProfileDraftApplyDecisionRequest,
    RejectProfileDraftRequest,
)
from app.application.profile_agent_orchestration import ProfileAgentRunService
from app.domain.users.models import UserAccount

router = APIRouter(tags=["profile-agent-runs"])


@router.post(
    "/profile-agent/runs",
    response_model=ProfileAgentRunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_profile_agent_run(
    payload: CreateProfileAgentRunRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileAgentRunService,
        Depends(get_profile_agent_run_service),
    ],
) -> ProfileAgentRunCreateResponse:
    result = await service.create_run(
        user,
        client_request_id=payload.client_request_id,
        user_message=payload.user_message,
        current_week=payload.current_week,
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return ProfileAgentRunCreateResponse(
        run_id=result.run.id,
        status=result.run.status,
        status_url=f"/api/v1/profile-agent/runs/{result.run.id}",
    )


@router.get("/profile-agent/runs/{run_id}", response_model=PlanningRunResponse)
async def get_profile_agent_run(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileAgentRunService,
        Depends(get_profile_agent_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(await service.get_run(user, run_id))


@router.get(
    "/profile-agent/runs/{run_id}/steps",
    response_model=list[AgentStepResponse],
)
async def list_profile_agent_run_steps(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileAgentRunService,
        Depends(get_profile_agent_run_service),
    ],
) -> list[AgentStepResponse]:
    return [
        AgentStepResponse.from_domain(item)
        for item in await service.list_steps(user, run_id)
    ]


@router.get(
    "/profile-agent/runs/{run_id}/checkpoints",
    response_model=list[StepCheckpointResponse],
)
async def list_profile_agent_run_checkpoints(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileAgentRunService,
        Depends(get_profile_agent_run_service),
    ],
) -> list[StepCheckpointResponse]:
    return [
        StepCheckpointResponse.from_domain(item)
        for item in await service.list_checkpoints(user, run_id)
    ]


@router.get(
    "/profile-agent/runs/{run_id}/audit",
    response_model=list[AuditEventResponse],
)
async def list_profile_agent_run_audit(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileAgentRunService,
        Depends(get_profile_agent_run_service),
    ],
) -> list[AuditEventResponse]:
    return [
        AuditEventResponse.from_domain(item)
        for item in await service.list_audit(user, run_id)
    ]


@router.post(
    "/profile-agent/runs/{run_id}/apply",
    response_model=PlanningRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_profile_agent_run_apply(
    run_id: UUID,
    payload: ProfileDraftApplyDecisionRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileAgentRunService,
        Depends(get_profile_agent_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(
        await service.submit_apply(user, run_id, payload.to_domain())
    )


@router.post(
    "/profile-agent/runs/{run_id}/reject",
    response_model=PlanningRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reject_profile_agent_run(
    run_id: UUID,
    payload: RejectProfileDraftRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ProfileAgentRunService,
        Depends(get_profile_agent_run_service),
    ],
) -> PlanningRunResponse:
    run, _ = await service.reject_run(
        user,
        run_id,
        client_request_id=payload.client_request_id,
        expected_draft_version=payload.expected_draft_version,
    )
    return PlanningRunResponse.from_domain(run)
