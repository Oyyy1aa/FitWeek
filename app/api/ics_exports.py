"""Deterministic ICS export metadata and download endpoints."""

from datetime import datetime
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import get_current_user, get_ics_export_service
from app.application.ics_export import CreateIcsExportCommand, IcsExportService
from app.domain.ics.models import IcsExportResult
from app.domain.users.models import UserAccount

router = APIRouter(tags=["ics-exports"])


class CreateIcsExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=128)
    expected_plan_version: int = Field(ge=1)


class IcsExportResponse(BaseModel):
    id: UUID
    client_request_id: str
    request_fingerprint: str
    root_plan_id: UUID
    revision: int
    plan_version: int
    policy_version: str
    content_sha256: str
    event_count: int
    byte_size: int
    filename: str
    created_at: datetime

    @classmethod
    def from_domain(cls, value: IcsExportResult) -> Self:
        return cls(**{name: getattr(value, name) for name in cls.model_fields})


@router.post(
    "/plans/{root_plan_id}/revisions/{revision}/ics-export",
    response_model=IcsExportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_ics_export(
    root_plan_id: UUID,
    revision: int,
    payload: CreateIcsExportRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[IcsExportService, Depends(get_ics_export_service)],
) -> IcsExportResponse:
    record, created = await service.create(
        user,
        root_plan_id,
        revision,
        CreateIcsExportCommand(**payload.model_dump()),
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return IcsExportResponse.from_domain(record.result)


@router.get("/ics-exports/{export_id}", response_model=IcsExportResponse)
async def get_ics_export(
    export_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[IcsExportService, Depends(get_ics_export_service)],
) -> IcsExportResponse:
    return IcsExportResponse.from_domain((await service.get(user, export_id)).result)


@router.get("/ics-exports/{export_id}/download")
async def download_ics_export(
    export_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[IcsExportService, Depends(get_ics_export_service)],
) -> Response:
    record = await service.get(user, export_id)
    return Response(
        content=record.content,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{record.result.filename}"'
        },
    )
