"""Phase 4A Context build and privacy-preserving Audit endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.context_contracts import (
    BuildContextRequest,
    BuiltContextResponse,
    ContextAuditResponse,
    ContextSnapshotReferenceResponse,
)
from app.api.dependencies import get_context_application_service, get_current_user
from app.application.contexts import ContextApplicationService
from app.config import Settings, get_settings
from app.domain.context.models import ContextBuildCommand
from app.domain.memory.errors import ContextDebugApiDisabledError
from app.domain.users.models import UserAccount

router = APIRouter(prefix="/contexts", tags=["contexts"])
snapshots_router = APIRouter(prefix="/context-snapshots", tags=["contexts"])


@router.post("/build", response_model=BuiltContextResponse)
async def build_context(
    payload: BuildContextRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ContextApplicationService, Depends(get_context_application_service)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BuiltContextResponse:
    if not settings.context_debug_api_enabled:
        raise ContextDebugApiDisabledError("The Context debug API is disabled.")
    return BuiltContextResponse.from_domain(
        await service.build(
            user,
            ContextBuildCommand(
                agent_type=payload.agent_type,
                current_task=payload.current_task,
                recent_behavior_summary=payload.recent_behavior_summary,
                catalog_reference=payload.catalog_reference,
                max_characters=payload.max_characters,
            ),
        )
    )


@router.get("/audits/{audit_id}", response_model=ContextAuditResponse)
async def get_context_audit(
    audit_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ContextApplicationService, Depends(get_context_application_service)
    ],
) -> ContextAuditResponse:
    return ContextAuditResponse.from_domain(await service.get_audit(user, audit_id))


@snapshots_router.get(
    "/{snapshot_id}",
    response_model=ContextSnapshotReferenceResponse,
)
async def get_context_snapshot(
    snapshot_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[
        ContextApplicationService, Depends(get_context_application_service)
    ],
) -> ContextSnapshotReferenceResponse:
    return ContextSnapshotReferenceResponse.from_domain(
        await service.get_snapshot_reference(user, snapshot_id)
    )
