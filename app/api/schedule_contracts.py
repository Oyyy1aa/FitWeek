"""Strict public contracts for read-only Schedule Draft review."""

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.common import LocationType
from app.domain.scheduling.enums import (
    BusyIntervalSource,
    CalendarReadMode,
    CalendarVerificationStatus,
    ScheduleDraftOutcome,
    ScheduleDraftSource,
    ScheduleDraftStatus,
)
from app.domain.scheduling.models import (
    BusySnapshot,
    ScheduleDraft,
    ScheduleTrace,
    TimeSlotCandidateSet,
)


class AvailabilityWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime
    location: LocationType


class BusyWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime


class CreateScheduleDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=120)
    root_plan_id: UUID
    source_revision: int = Field(ge=1)
    expected_plan_version: int = Field(ge=1)
    timezone: str = Field(min_length=1, max_length=100)
    availability_windows: tuple[AvailabilityWindowRequest, ...] = Field(
        min_length=1, max_length=64
    )
    manual_busy_windows: tuple[BusyWindowRequest, ...] = Field(
        default=(), max_length=128
    )
    target_session_ids: tuple[UUID, ...] | None = Field(default=None, max_length=16)

    @field_validator("client_request_id", "timezone")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class ReviewScheduleDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class ScheduleAssignmentResponse(BaseModel):
    session_id: UUID
    slot_id: str
    scheduled_start: datetime
    scheduled_end: datetime
    location: LocationType
    timezone: str
    start_local: datetime
    end_local: datetime


class UnresolvedSessionResponse(BaseModel):
    session_id: UUID
    code: str
    message: str


class ScheduleDraftResponse(BaseModel):
    id: UUID
    request_id: UUID
    client_request_id: str
    status: ScheduleDraftStatus
    version: int
    outcome: ScheduleDraftOutcome
    source: ScheduleDraftSource
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    timezone: str
    busy_snapshot_id: UUID
    candidate_set_id: UUID
    candidate_set_fingerprint: str
    context_snapshot_reference_id: UUID
    context_fingerprint: str
    assignments: tuple[ScheduleAssignmentResponse, ...]
    unresolved: tuple[UnresolvedSessionResponse, ...]
    prompt_version: str
    fallback_used: bool
    calendar_verification_status: CalendarVerificationStatus
    explanation_summary: str
    created_at: datetime
    expires_at: datetime
    reviewed_at: datetime | None

    @classmethod
    def from_domain(cls, value: ScheduleDraft) -> "ScheduleDraftResponse":
        return cls(
            id=value.id,
            request_id=value.request_id,
            client_request_id=value.client_request_id,
            status=value.status,
            version=value.version,
            outcome=value.outcome,
            source=value.source,
            root_plan_id=value.root_plan_id,
            source_revision=value.source_revision,
            source_plan_version=value.source_plan_version,
            timezone=value.timezone,
            busy_snapshot_id=value.busy_snapshot_id,
            candidate_set_id=value.candidate_set_id,
            candidate_set_fingerprint=value.candidate_set_fingerprint,
            context_snapshot_reference_id=value.context_snapshot_reference_id,
            context_fingerprint=value.context_fingerprint,
            assignments=tuple(
                ScheduleAssignmentResponse(
                    session_id=item.session_id,
                    slot_id=item.slot_id,
                    scheduled_start=item.scheduled_start,
                    scheduled_end=item.scheduled_end,
                    location=item.location,
                    timezone=value.timezone,
                    start_local=item.scheduled_start.astimezone(
                        ZoneInfo(value.timezone)
                    ),
                    end_local=item.scheduled_end.astimezone(ZoneInfo(value.timezone)),
                )
                for item in value.assignments
            ),
            unresolved=tuple(
                UnresolvedSessionResponse(
                    session_id=item.session_id,
                    code=item.code,
                    message=item.message,
                )
                for item in value.unresolved
            ),
            prompt_version=value.prompt_version,
            fallback_used=value.fallback_used,
            calendar_verification_status=value.calendar_verification_status,
            explanation_summary=value.explanation_summary,
            created_at=value.created_at,
            expires_at=value.expires_at,
            reviewed_at=value.reviewed_at,
        )


class BusyIntervalResponse(BaseModel):
    start: datetime
    end: datetime
    source: BusyIntervalSource


class BusySnapshotResponse(BaseModel):
    id: UUID
    timezone: str
    mode: CalendarReadMode
    verification_status: CalendarVerificationStatus
    range_start_utc: datetime
    range_end_utc: datetime
    intervals: tuple[BusyIntervalResponse, ...]
    fingerprint: str
    provider_summary: str
    provider_name: str
    provider_version: str
    created_at: datetime

    @classmethod
    def from_domain(cls, value: BusySnapshot) -> "BusySnapshotResponse":
        return cls(
            id=value.id,
            timezone=value.timezone,
            mode=value.mode,
            verification_status=value.verification_status,
            range_start_utc=value.range_start_utc,
            range_end_utc=value.range_end_utc,
            intervals=tuple(
                BusyIntervalResponse(
                    start=item.start,
                    end=item.end,
                    source=item.source,
                )
                for item in value.intervals
            ),
            fingerprint=value.fingerprint,
            provider_summary=value.provider_summary,
            provider_name=value.provider_name,
            provider_version=value.provider_version,
            created_at=value.created_at,
        )


class CandidateResponse(BaseModel):
    id: UUID
    session_id: UUID
    slot_id: str
    start: datetime
    end: datetime
    location: LocationType
    timezone: str
    start_local: datetime
    end_local: datetime
    source_availability_id: UUID


class CandidateSetResponse(BaseModel):
    id: UUID
    fingerprint: str
    policy_version: str
    slots: tuple[CandidateResponse, ...]

    @classmethod
    def from_domain(cls, value: TimeSlotCandidateSet) -> "CandidateSetResponse":
        return cls(
            id=value.id,
            fingerprint=value.fingerprint,
            policy_version=value.policy_version,
            slots=tuple(
                CandidateResponse(
                    id=item.id,
                    session_id=item.session_id,
                    slot_id=item.slot_id,
                    start=item.start,
                    end=item.end,
                    location=item.location,
                    timezone=item.timezone,
                    start_local=item.start_local,
                    end_local=item.end_local,
                    source_availability_id=item.source_availability_id,
                )
                for item in value.slots
            ),
        )


class ScheduleTraceResponse(BaseModel):
    draft_id: UUID
    request_id: UUID
    busy_snapshot_id: UUID
    candidate_set_id: UUID
    candidate_set_fingerprint: str
    context_snapshot_reference_id: UUID
    context_fingerprint: str
    prompt_version: str
    provider_summary: str
    timezone: str
    provider_name: str
    provider_version: str
    attempt_no: int
    outcome: str
    validation_error_code: str | None
    latency_ms: float
    source: ScheduleDraftSource
    fallback_used: bool
    provider_attempts: int
    calendar_mode: CalendarReadMode
    calendar_attempts: int
    model_trace_ids: tuple[UUID, ...]
    created_at: datetime

    @classmethod
    def from_domain(cls, value: ScheduleTrace) -> "ScheduleTraceResponse":
        return cls(
            draft_id=value.draft_id,
            request_id=value.request_id,
            busy_snapshot_id=value.busy_snapshot_id,
            candidate_set_id=value.candidate_set_id,
            candidate_set_fingerprint=value.candidate_set_fingerprint,
            context_snapshot_reference_id=value.context_snapshot_reference_id,
            context_fingerprint=value.context_fingerprint,
            prompt_version=value.prompt_version,
            provider_summary=value.provider_summary,
            timezone=value.timezone,
            provider_name=value.provider_name,
            provider_version=value.provider_version,
            attempt_no=value.attempt_no,
            outcome=value.outcome,
            validation_error_code=value.validation_error_code,
            latency_ms=value.latency_ms,
            source=value.source,
            fallback_used=value.fallback_used,
            provider_attempts=value.provider_attempts,
            calendar_mode=value.calendar_mode,
            calendar_attempts=value.calendar_attempts,
            model_trace_ids=value.model_trace_ids,
            created_at=value.created_at,
        )


class ScheduleMetricsResponse(BaseModel):
    schedule_draft_requests: int
    calendar_read_requests: int
    calendar_read_successes: int
    calendar_read_failures: int
    calendar_manual_degraded: int
    schedule_agent_primary_successes: int
    schedule_agent_backup_successes: int
    schedule_deterministic_fallbacks: int
    schedule_complete_drafts: int
    schedule_partial_drafts: int
    schedule_conflicts_rejected: int
    schedule_timezone_failures: int
    schedule_idempotent_reuses: int
    schedule_idempotency_conflicts: int
    schedule_drafts_accepted: int
    schedule_drafts_rejected: int
