"""Control-plane API for Session Design Plan application Runs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import (
    get_current_user,
    get_session_design_application_run_service,
)
from app.api.orchestration_contracts import (
    AgentStepResponse,
    AuditEventResponse,
    PlanningRunResponse,
    StepCheckpointResponse,
)
from app.application.session_design_orchestration import (
    SessionDesignApplicationRunService,
)
from app.domain.users.models import UserAccount

router = APIRouter(tags=["session-design-application-runs"])


class CreateSessionDesignApplicationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=128)
    draft_id: UUID
    root_plan_id: UUID
    source_revision: int = Field(ge=1)
    expected_plan_version: int = Field(ge=1)
    target_session_id: UUID


class ConfirmSessionDesignApplicationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    expected_plan_version: int = Field(ge=1)


@router.post(
    "/session-design-application-runs",
    response_model=PlanningRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_session_design_application_run(
    payload: CreateSessionDesignApplicationRunRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SessionDesignApplicationRunService,
        Depends(get_session_design_application_run_service),
    ],
) -> PlanningRunResponse:
    result = await service.create_run(user, **payload.model_dump())
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return PlanningRunResponse.from_domain(result.run)


@router.get(
    "/session-design-application-runs/{run_id}",
    response_model=PlanningRunResponse,
)
async def get_session_design_application_run(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SessionDesignApplicationRunService,
        Depends(get_session_design_application_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(await service.get_run(user, run_id))


@router.get(
    "/session-design-application-runs/{run_id}/steps",
    response_model=list[AgentStepResponse],
)
async def list_session_design_application_run_steps(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SessionDesignApplicationRunService,
        Depends(get_session_design_application_run_service),
    ],
) -> list[AgentStepResponse]:
    return [
        AgentStepResponse.from_domain(item)
        for item in await service.list_steps(user, run_id)
    ]


@router.get(
    "/session-design-application-runs/{run_id}/checkpoints",
    response_model=list[StepCheckpointResponse],
)
async def list_session_design_application_run_checkpoints(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SessionDesignApplicationRunService,
        Depends(get_session_design_application_run_service),
    ],
) -> list[StepCheckpointResponse]:
    return [
        StepCheckpointResponse.from_domain(item)
        for item in await service.list_checkpoints(user, run_id)
    ]


@router.get(
    "/session-design-application-runs/{run_id}/audit",
    response_model=list[AuditEventResponse],
)
async def list_session_design_application_run_audit(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SessionDesignApplicationRunService,
        Depends(get_session_design_application_run_service),
    ],
) -> list[AuditEventResponse]:
    return [
        AuditEventResponse.from_domain(item)
        for item in await service.list_audit(user, run_id)
    ]


@router.post(
    "/session-design-application-runs/{run_id}/confirm",
    response_model=PlanningRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_session_design_application_run(
    run_id: UUID,
    payload: ConfirmSessionDesignApplicationRunRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SessionDesignApplicationRunService,
        Depends(get_session_design_application_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(
        await service.confirm_run(user, run_id, **payload.model_dump())
    )


@router.post(
    "/session-design-application-runs/{run_id}/cancel",
    response_model=PlanningRunResponse,
)
async def cancel_session_design_application_run(
    run_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        SessionDesignApplicationRunService,
        Depends(get_session_design_application_run_service),
    ],
) -> PlanningRunResponse:
    return PlanningRunResponse.from_domain(await service.cancel_run(user, run_id))
