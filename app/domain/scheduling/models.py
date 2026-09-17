"""Immutable busy snapshots, candidate sets, Agent output, and Schedule Drafts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.common import (
    DomainValidationError,
    LocationType,
    require_non_blank,
    require_utc_datetime,
    require_version,
)
from app.domain.context.enums import ContextDegradedMode
from app.domain.scheduling.enums import (
    BusyIntervalSource,
    CalendarReadMode,
    CalendarVerificationStatus,
    ScheduleDraftOutcome,
    ScheduleDraftSource,
    ScheduleDraftStatus,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AvailabilityWindow:
    start: datetime
    end: datetime
    location: LocationType
    id: UUID | None = None

    def __post_init__(self) -> None:
        require_utc_datetime(self.start, "start")
        require_utc_datetime(self.end, "end")
        if self.start >= self.end:
            raise DomainValidationError("availability interval must be positive.")
        if self.id is None:
            raw = (
                f"{self.start.isoformat()}:{self.end.isoformat()}:{self.location.value}"
            )
            object.__setattr__(self, "id", uuid5(NAMESPACE_URL, raw))


@dataclass(frozen=True, slots=True, kw_only=True)
class ZonedInterval:
    start_utc: datetime
    end_utc: datetime
    timezone: str

    def __post_init__(self) -> None:
        require_utc_datetime(self.start_utc, "start_utc")
        require_utc_datetime(self.end_utc, "end_utc")
        require_non_blank(self.timezone, "timezone")
        if self.start_utc >= self.end_utc:
            raise DomainValidationError("zoned interval must be positive.")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise DomainValidationError(
                "timezone must be a valid IANA timezone.", code="INVALID_TIMEZONE"
            ) from exc

    @property
    def start_local(self) -> datetime:
        return self.start_utc.astimezone(ZoneInfo(self.timezone))

    @property
    def end_local(self) -> datetime:
        return self.end_utc.astimezone(ZoneInfo(self.timezone))


@dataclass(frozen=True, slots=True, kw_only=True, order=True)
class BusyInterval:
    start: datetime
    end: datetime
    source: BusyIntervalSource

    def __post_init__(self) -> None:
        require_utc_datetime(self.start, "start")
        require_utc_datetime(self.end, "end")
        if self.start >= self.end:
            raise DomainValidationError("busy interval must be positive.")


@dataclass(frozen=True, slots=True, kw_only=True)
class BusySnapshot:
    id: UUID
    user_id: UUID
    timezone: str
    range_start_utc: datetime
    range_end_utc: datetime
    mode: CalendarReadMode
    verification_status: CalendarVerificationStatus
    intervals: tuple[BusyInterval, ...]
    fingerprint: str
    provider_summary: str
    provider_name: str
    provider_version: str
    created_at: datetime

    def __post_init__(self) -> None:
        require_non_blank(self.timezone, "timezone")
        require_non_blank(self.fingerprint, "fingerprint")
        require_non_blank(self.provider_summary, "provider_summary")
        require_non_blank(self.provider_name, "provider_name")
        require_non_blank(self.provider_version, "provider_version")
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.range_start_utc, "range_start_utc")
        require_utc_datetime(self.range_end_utc, "range_end_utc")
        if self.range_start_utc >= self.range_end_utc:
            raise DomainValidationError("busy Snapshot range must be positive.")
        if self.intervals != tuple(sorted(self.intervals)):
            raise DomainValidationError("busy intervals must use stable ordering.")


@dataclass(frozen=True, slots=True, kw_only=True, order=True)
class TimeSlotCandidate:
    start: datetime
    session_id: UUID
    slot_id: str
    end: datetime
    location: LocationType
    preference_score: int = 0
    id: UUID = field(default_factory=lambda: uuid5(NAMESPACE_URL, "unset-slot"))
    timezone: str = "UTC"
    source_availability_id: UUID = field(
        default_factory=lambda: uuid5(NAMESPACE_URL, "unset-availability")
    )

    def __post_init__(self) -> None:
        require_non_blank(self.slot_id, "slot_id")
        require_utc_datetime(self.start, "start")
        require_utc_datetime(self.end, "end")
        if self.start >= self.end:
            raise DomainValidationError("candidate interval must be positive.")
        require_non_blank(self.timezone, "timezone")

    @property
    def start_local(self) -> datetime:
        return self.start.astimezone(ZoneInfo(self.timezone))

    @property
    def end_local(self) -> datetime:
        return self.end.astimezone(ZoneInfo(self.timezone))


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionSlotCandidates:
    session_id: UUID
    slot_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.slot_ids or len(self.slot_ids) != len(set(self.slot_ids)):
            raise DomainValidationError(
                "session candidate IDs must be unique/non-empty."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeSlotCandidateSet:
    id: UUID
    user_id: UUID
    root_plan_id: UUID
    source_revision: int
    busy_snapshot_id: UUID
    context_snapshot_reference_id: UUID
    slots: tuple[TimeSlotCandidate, ...]
    session_candidates: tuple[SessionSlotCandidates, ...]
    fingerprint: str
    policy_version: str
    created_at: datetime

    def __post_init__(self) -> None:
        require_non_blank(self.fingerprint, "fingerprint")
        require_non_blank(self.policy_version, "policy_version")
        require_utc_datetime(self.created_at, "created_at")
        ids = tuple(item.slot_id for item in self.slots)
        if len(ids) != len(set(ids)):
            raise DomainValidationError("candidate slot IDs must be unique.")
        allowed = set(ids)
        if any(not set(item.slot_ids) <= allowed for item in self.session_candidates):
            raise DomainValidationError("session candidates reference unknown slots.")

    @property
    def slot_map(self) -> dict[str, TimeSlotCandidate]:
        return {item.slot_id: item for item in self.slots}

    @property
    def allowed_by_session(self) -> dict[UUID, tuple[str, ...]]:
        return {item.session_id: item.slot_ids for item in self.session_candidates}


class ScheduleAssignmentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID
    slot_id: str = Field(min_length=1, max_length=120)

    @field_validator("slot_id")
    @classmethod
    def strip_slot_id(cls, value: str) -> str:
        return value.strip()


class ScheduleAgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assignments: tuple[ScheduleAssignmentOutput, ...]
    unresolved_session_ids: tuple[UUID, ...]
    explanation_summary: str = Field(min_length=1, max_length=600)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleAssignment:
    session_id: UUID
    slot_id: str
    scheduled_start: datetime
    scheduled_end: datetime
    location: LocationType

    def __post_init__(self) -> None:
        require_non_blank(self.slot_id, "slot_id")
        require_utc_datetime(self.scheduled_start, "scheduled_start")
        require_utc_datetime(self.scheduled_end, "scheduled_end")
        if self.scheduled_start >= self.scheduled_end:
            raise DomainValidationError("assignment interval must be positive.")


@dataclass(frozen=True, slots=True, kw_only=True)
class UnresolvedSession:
    session_id: UUID
    code: str
    message: str

    def __post_init__(self) -> None:
        require_non_blank(self.code, "code")
        require_non_blank(self.message, "message")


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleDraft:
    id: UUID
    request_id: UUID
    client_request_id: str
    user_id: UUID
    request_payload_fingerprint: str
    request_fingerprint: str
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    timezone: str
    busy_snapshot_id: UUID
    candidate_set_id: UUID
    candidate_set_fingerprint: str
    context_snapshot_reference_id: UUID
    context_fingerprint: str
    context_degraded_mode: ContextDegradedMode
    assignments: tuple[ScheduleAssignment, ...]
    unresolved: tuple[UnresolvedSession, ...]
    outcome: ScheduleDraftOutcome
    source: ScheduleDraftSource
    prompt_version: str
    provider_summary: str
    fallback_used: bool
    calendar_verification_status: CalendarVerificationStatus
    explanation_summary: str
    created_at: datetime
    expires_at: datetime
    status: ScheduleDraftStatus = ScheduleDraftStatus.PENDING_REVIEW
    version: int = 1
    reviewed_at: datetime | None = None
    applied_root_plan_id: UUID | None = None
    applied_source_revision: int | None = None
    applied_created_revision: int | None = None
    application_result_id: UUID | None = None
    applied_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "client_request_id",
            "request_payload_fingerprint",
            "request_fingerprint",
            "timezone",
            "candidate_set_fingerprint",
            "context_fingerprint",
            "prompt_version",
            "provider_summary",
            "explanation_summary",
        ):
            require_non_blank(getattr(self, field_name), field_name)
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.expires_at, "expires_at")
        require_version(self.version)
        if self.expires_at <= self.created_at:
            raise DomainValidationError("expires_at must be later than created_at.")
        assignment_ids = tuple(item.session_id for item in self.assignments)
        unresolved_ids = tuple(item.session_id for item in self.unresolved)
        if len(assignment_ids) != len(set(assignment_ids)):
            raise DomainValidationError("a Session can be assigned only once.")
        if set(assignment_ids) & set(unresolved_ids):
            raise DomainValidationError("a Session cannot be assigned and unresolved.")
        expected = (
            ScheduleDraftOutcome.COMPLETE
            if not self.unresolved
            else ScheduleDraftOutcome.PARTIAL
        )
        if self.outcome is not expected:
            raise DomainValidationError(
                "Draft outcome does not match unresolved items."
            )
        if self.status is ScheduleDraftStatus.PENDING_REVIEW:
            if self.reviewed_at is not None:
                raise DomainValidationError("pending Draft cannot have reviewed_at.")
        elif self.reviewed_at is None:
            raise DomainValidationError("terminal Draft requires reviewed_at.")
        if self.status is ScheduleDraftStatus.APPLIED:
            if any(
                value is None
                for value in (
                    self.applied_root_plan_id,
                    self.applied_source_revision,
                    self.applied_created_revision,
                    self.application_result_id,
                    self.applied_at,
                )
            ):
                raise DomainValidationError(
                    "APPLIED Draft requires application audit references."
                )
            assert self.applied_at is not None
            require_utc_datetime(self.applied_at, "applied_at")
        elif any(
            value is not None
            for value in (
                self.applied_root_plan_id,
                self.applied_source_revision,
                self.applied_created_revision,
                self.application_result_id,
                self.applied_at,
            )
        ):
            raise DomainValidationError(
                "only an APPLIED Draft may have application references."
            )

    def accept(self, at: datetime) -> ScheduleDraft:
        if self.outcome is not ScheduleDraftOutcome.COMPLETE:
            raise DomainValidationError("a PARTIAL Schedule Draft cannot be accepted.")
        return self._review(ScheduleDraftStatus.ACCEPTED, at)

    def reject(self, at: datetime) -> ScheduleDraft:
        return self._review(ScheduleDraftStatus.REJECTED, at)

    def expire(self, at: datetime) -> ScheduleDraft:
        return self._review(ScheduleDraftStatus.EXPIRED, at)

    def mark_applied(
        self,
        *,
        root_plan_id: UUID,
        source_revision: int,
        created_revision: int,
        application_result_id: UUID,
        at: datetime,
    ) -> ScheduleDraft:
        require_utc_datetime(at, "applied_at")
        if self.status is not ScheduleDraftStatus.ACCEPTED:
            raise DomainValidationError(
                "only an ACCEPTED Schedule Draft can be applied."
            )
        if self.outcome is not ScheduleDraftOutcome.COMPLETE:
            raise DomainValidationError("a PARTIAL Schedule Draft cannot be applied.")
        if created_revision != source_revision + 1:
            raise DomainValidationError(
                "created revision must follow the source revision."
            )
        return replace(
            self,
            status=ScheduleDraftStatus.APPLIED,
            applied_root_plan_id=root_plan_id,
            applied_source_revision=source_revision,
            applied_created_revision=created_revision,
            application_result_id=application_result_id,
            applied_at=at,
            version=self.version + 1,
        )

    def _review(self, status: ScheduleDraftStatus, at: datetime) -> ScheduleDraft:
        require_utc_datetime(at, "reviewed_at")
        if self.status is not ScheduleDraftStatus.PENDING_REVIEW:
            raise DomainValidationError(
                "only a pending Schedule Draft can be reviewed."
            )
        return replace(self, status=status, reviewed_at=at, version=self.version + 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleTrace:
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

    def __post_init__(self) -> None:
        require_utc_datetime(self.created_at, "created_at")
        require_non_blank(self.timezone, "timezone")
        require_non_blank(self.provider_name, "provider_name")
        require_non_blank(self.provider_version, "provider_version")
        require_non_blank(self.outcome, "outcome")
        if self.attempt_no < 0 or self.latency_ms < 0:
            raise DomainValidationError("trace counters cannot be negative.")


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateScheduleDraftCommand:
    client_request_id: str
    root_plan_id: UUID
    source_revision: int
    expected_plan_version: int
    timezone: str
    availability_windows: tuple[AvailabilityWindow, ...]
    manual_busy_windows: tuple[BusyInterval, ...] = ()
    target_session_ids: tuple[UUID, ...] | None = None

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        require_non_blank(self.timezone, "timezone")
        require_version(self.source_revision)
        require_version(self.expected_plan_version)
        if not self.availability_windows or len(self.availability_windows) > 64:
            raise DomainValidationError(
                "availability_windows must contain 1 to 64 items."
            )
        if len(self.manual_busy_windows) > 128:
            raise DomainValidationError("manual_busy_windows exceeds the safe limit.")
        if self.target_session_ids is not None:
            if not self.target_session_ids:
                raise DomainValidationError("target_session_ids cannot be empty.")
            if len(self.target_session_ids) != len(set(self.target_session_ids)):
                raise DomainValidationError("target_session_ids must be unique.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleMetricsSnapshot:
    values: MappingProxyType[str, int]
