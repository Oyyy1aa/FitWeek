"""Control-plane API for Phase 7B Recovery application Runs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import (
    get_current_user,
    get_recovery_application_run_service,
)
from app.api.orchestration_contracts import (
    AgentStepResponse,
    AuditEventResponse,
    PlanningRunResponse,
    StepCheckpointResponse,
)
from app.application.recovery_application_orchestration import (
    RecoveryApplicationRunService,
)
from app.domain.users.models import UserAccount

router = APIRouter(tags=["recovery-application-runs"])


class CreateRecoveryApplicationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=128)
    recovery_draft_id: UUID
    expected_draft_version: int = Field(ge=1)
    root_plan_id: UUID
    source_revision: int = Field(ge=1)
    expected_plan_version: int = Field(ge=1)


class ConfirmRecoveryApplicationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    expected_plan_version: int = Field(ge=1)


class RecoveryRunSubdraftsResponse(BaseModel):
    session_design_draft_ids: tuple[UUID, ...]
    schedule_draft_ids: tuple[UUID, ...]


@router.post(
    "/recovery-application-runs",
    response_model=PlanningRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_recovery_application_run(
    payload: CreateRecoveryApplicationRunRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationRunService,
        Depends(get_recovery_application_run_service),
    ],
) -> PlanningRunResponse:
    result = await service.create_run(user, **payload.model_dump())
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return PlanningRunResponse.from_domain(result.run)


@router.get("/recovery-application-runs/{run_id}", response_model=PlanningRunResponse)
async def get_recovery_application_run(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationRunService,
        Depends(get_recovery_application_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(await service.get_run(user, run_id))


@router.get(
    "/recovery-application-runs/{run_id}/steps",
    response_model=list[AgentStepResponse],
)
async def list_recovery_application_run_steps(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationRunService,
        Depends(get_recovery_application_run_service),
    ],
) -> list[AgentStepResponse]:
    return [
        AgentStepResponse.from_domain(item)
        for item in await service.list_steps(user, run_id)
    ]


@router.get(
    "/recovery-application-runs/{run_id}/checkpoints",
    response_model=list[StepCheckpointResponse],
)
async def list_recovery_application_run_checkpoints(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationRunService,
        Depends(get_recovery_application_run_service),
    ],
) -> list[StepCheckpointResponse]:
    return [
        StepCheckpointResponse.from_domain(item)
        for item in await service.list_checkpoints(user, run_id)
    ]


@router.get(
    "/recovery-application-runs/{run_id}/audit",
    response_model=list[AuditEventResponse],
)
async def list_recovery_application_run_audit(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationRunService,
        Depends(get_recovery_application_run_service),
    ],
) -> list[AuditEventResponse]:
    return [
        AuditEventResponse.from_domain(item)
        for item in await service.list_audit(user, run_id)
    ]


@router.get(
    "/recovery-application-runs/{run_id}/subdrafts",
    response_model=RecoveryRunSubdraftsResponse,
)
async def get_recovery_application_run_subdrafts(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationRunService,
        Depends(get_recovery_application_run_service),
    ],
) -> RecoveryRunSubdraftsResponse:
    value = await service.subdrafts(user, run_id)
    return RecoveryRunSubdraftsResponse(
        session_design_draft_ids=value.session_design_draft_ids,
        schedule_draft_ids=value.schedule_draft_ids,
    )


@router.post(
    "/recovery-application-runs/{run_id}/continue",
    response_model=PlanningRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def continue_recovery_application_run(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationRunService,
        Depends(get_recovery_application_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(await service.continue_run(user, run_id))


@router.post(
    "/recovery-application-runs/{run_id}/confirm",
    response_model=PlanningRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_recovery_application_run(
    run_id: UUID,
    payload: ConfirmRecoveryApplicationRunRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationRunService,
        Depends(get_recovery_application_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(
        await service.confirm_run(user, run_id, **payload.model_dump())
    )


@router.post(
    "/recovery-application-runs/{run_id}/cancel",
    response_model=PlanningRunResponse,
)
async def cancel_recovery_application_run(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        RecoveryApplicationRunService,
        Depends(get_recovery_application_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(await service.cancel_run(user, run_id))
