"""Phase 2A asynchronous planning-run control-plane endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import get_current_user, get_orchestration_service
from app.api.orchestration_contracts import (
    AgentStepResponse,
    AuditEventResponse,
    OrchestratorMetricsResponse,
    PlanningRunConfirmRequest,
    PlanningRunCreateRequest,
    PlanningRunCreateResponse,
    PlanningRunResponse,
    StepCheckpointResponse,
)
from app.application.orchestration import OrchestrationService
from app.domain.users.models import UserAccount

router = APIRouter(tags=["planning-runs"])


@router.post(
    "/planning-runs",
    response_model=PlanningRunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_planning_run(
    payload: PlanningRunCreateRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[OrchestrationService, Depends(get_orchestration_service)],
) -> PlanningRunCreateResponse:
    result = await service.create_run(
        user,
        client_request_id=payload.client_request_id,
        command=payload.to_command(),
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return PlanningRunCreateResponse(
        run_id=result.run.id,
        status=result.run.status,
        status_url=f"/api/v1/planning-runs/{result.run.id}",
    )


@router.get("/planning-runs/{run_id}", response_model=PlanningRunResponse)
async def get_planning_run(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[OrchestrationService, Depends(get_orchestration_service)],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(await service.get_run(user, run_id))


@router.get(
    "/planning-runs/{run_id}/steps",
    response_model=list[AgentStepResponse],
)
async def list_planning_steps(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[OrchestrationService, Depends(get_orchestration_service)],
) -> list[AgentStepResponse]:
    return [
        AgentStepResponse.from_domain(item)
        for item in await service.list_steps(user, run_id)
    ]


@router.get(
    "/planning-runs/{run_id}/checkpoints",
    response_model=list[StepCheckpointResponse],
)
async def list_planning_checkpoints(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[OrchestrationService, Depends(get_orchestration_service)],
) -> list[StepCheckpointResponse]:
    return [
        StepCheckpointResponse.from_domain(item)
        for item in await service.list_checkpoints(user, run_id)
    ]


@router.get(
    "/planning-runs/{run_id}/audit",
    response_model=list[AuditEventResponse],
)
async def list_planning_audit(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[OrchestrationService, Depends(get_orchestration_service)],
) -> list[AuditEventResponse]:
    return [
        AuditEventResponse.from_domain(item)
        for item in await service.list_audit(user, run_id)
    ]


@router.post(
    "/planning-runs/{run_id}/confirm",
    response_model=PlanningRunResponse,
)
async def confirm_planning_run(
    run_id: UUID,
    payload: PlanningRunConfirmRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[OrchestrationService, Depends(get_orchestration_service)],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(
        await service.confirm_and_resume_run(
            user,
            run_id,
            expected_plan_version=payload.expected_plan_version,
        )
    )


@router.post(
    "/planning-runs/{run_id}/cancel",
    response_model=PlanningRunResponse,
)
async def cancel_planning_run(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[OrchestrationService, Depends(get_orchestration_service)],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(await service.cancel_run(user, run_id))


@router.get("/orchestrator/metrics", response_model=OrchestratorMetricsResponse)
async def get_orchestrator_metrics(
    service: Annotated[OrchestrationService, Depends(get_orchestration_service)],
) -> OrchestratorMetricsResponse:
    return OrchestratorMetricsResponse.from_domain(service.metrics())
