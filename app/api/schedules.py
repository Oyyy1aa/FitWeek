"""Controlled Schedule Draft endpoints; no Calendar or Plan write endpoint exists."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import get_current_user, get_schedule_draft_service
from app.api.schedule_contracts import (
    BusySnapshotResponse,
    CandidateSetResponse,
    CreateScheduleDraftRequest,
    ReviewScheduleDraftRequest,
    ScheduleDraftResponse,
    ScheduleMetricsResponse,
    ScheduleTraceResponse,
)
from app.application.schedules import ScheduleDraftService
from app.domain.scheduling.enums import BusyIntervalSource
from app.domain.scheduling.models import (
    AvailabilityWindow,
    BusyInterval,
    CreateScheduleDraftCommand,
)
from app.domain.users.models import UserAccount
from app.scheduling.time_policy import TimezonePolicy

router = APIRouter(tags=["schedule-drafts"])


@router.post("/schedule-drafts", response_model=ScheduleDraftResponse, status_code=201)
async def create_schedule_draft(
    payload: CreateScheduleDraftRequest,
    response: Response,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ScheduleDraftService, Depends(get_schedule_draft_service)],
) -> ScheduleDraftResponse:
    policy = TimezonePolicy()
    command = CreateScheduleDraftCommand(
        client_request_id=payload.client_request_id,
        root_plan_id=payload.root_plan_id,
        source_revision=payload.source_revision,
        expected_plan_version=payload.expected_plan_version,
        timezone=payload.timezone,
        availability_windows=tuple(
            AvailabilityWindow(
                start=policy.to_utc(item.start, payload.timezone, "availability.start"),
                end=policy.to_utc(item.end, payload.timezone, "availability.end"),
                location=item.location,
            )
            for item in payload.availability_windows
        ),
        manual_busy_windows=tuple(
            BusyInterval(
                start=policy.to_utc(item.start, payload.timezone, "manual_busy.start"),
                end=policy.to_utc(item.end, payload.timezone, "manual_busy.end"),
                source=BusyIntervalSource.MANUAL,
            )
            for item in payload.manual_busy_windows
        ),
        target_session_ids=payload.target_session_ids,
    )
    draft, reused = await service.create(user, command)
    if reused:
        response.status_code = status.HTTP_200_OK
    return ScheduleDraftResponse.from_domain(draft)


@router.get("/schedule-drafts/{draft_id}", response_model=ScheduleDraftResponse)
async def get_schedule_draft(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ScheduleDraftService, Depends(get_schedule_draft_service)],
) -> ScheduleDraftResponse:
    return ScheduleDraftResponse.from_domain(await service.get(user, draft_id))


@router.get("/schedule-drafts/{draft_id}/trace", response_model=ScheduleTraceResponse)
async def get_schedule_trace(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ScheduleDraftService, Depends(get_schedule_draft_service)],
) -> ScheduleTraceResponse:
    return ScheduleTraceResponse.from_domain(await service.trace(user, draft_id))


@router.get(
    "/schedule-drafts/{draft_id}/busy-snapshot", response_model=BusySnapshotResponse
)
async def get_busy_snapshot(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ScheduleDraftService, Depends(get_schedule_draft_service)],
) -> BusySnapshotResponse:
    return BusySnapshotResponse.from_domain(await service.busy_snapshot(user, draft_id))


@router.get(
    "/schedule-drafts/{draft_id}/candidate-set", response_model=CandidateSetResponse
)
async def get_candidate_set(
    draft_id: UUID,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ScheduleDraftService, Depends(get_schedule_draft_service)],
) -> CandidateSetResponse:
    return CandidateSetResponse.from_domain(await service.candidate_set(user, draft_id))


async def _review(
    *,
    accept: bool,
    draft_id: UUID,
    payload: ReviewScheduleDraftRequest,
    user: UserAccount,
    service: ScheduleDraftService,
) -> ScheduleDraftResponse:
    return ScheduleDraftResponse.from_domain(
        await service.review(
            user,
            draft_id,
            expected_version=payload.expected_version,
            accept=accept,
        )
    )


@router.post("/schedule-drafts/{draft_id}/accept", response_model=ScheduleDraftResponse)
async def accept_schedule_draft(
    draft_id: UUID,
    payload: ReviewScheduleDraftRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ScheduleDraftService, Depends(get_schedule_draft_service)],
) -> ScheduleDraftResponse:
    return await _review(
        accept=True, draft_id=draft_id, payload=payload, user=user, service=service
    )


@router.post("/schedule-drafts/{draft_id}/reject", response_model=ScheduleDraftResponse)
async def reject_schedule_draft(
    draft_id: UUID,
    payload: ReviewScheduleDraftRequest,
    user: Annotated[UserAccount, Depends(get_current_user)],
    service: Annotated[ScheduleDraftService, Depends(get_schedule_draft_service)],
) -> ScheduleDraftResponse:
    return await _review(
        accept=False, draft_id=draft_id, payload=payload, user=user, service=service
    )


@router.get("/schedule/metrics", response_model=ScheduleMetricsResponse)
async def get_schedule_metrics(
    service: Annotated[ScheduleDraftService, Depends(get_schedule_draft_service)],
) -> ScheduleMetricsResponse:
    return ScheduleMetricsResponse(**service.metrics())
