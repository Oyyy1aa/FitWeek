"""Immutable Schedule Draft application values and audit results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from app.domain.common import LocationType, require_non_blank, require_utc_datetime
from app.domain.scheduling.enums import CalendarVerificationStatus
from app.domain.scheduling.models import ScheduleDraft
from app.safety.models import SafetyViolation

if TYPE_CHECKING:
    from app.domain.plans.models import WeeklyPlan


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyScheduleDraftCommand:
    client_request_id: str
    expected_draft_version: int
    root_plan_id: UUID
    source_revision: int
    expected_plan_version: int

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        for name in (
            "expected_draft_version",
            "source_revision",
            "expected_plan_version",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleAssignmentChange:
    session_id: UUID
    old_start_utc: datetime
    old_end_utc: datetime
    new_start_utc: datetime
    new_end_utc: datetime
    timezone: str
    old_start_local: datetime
    new_start_local: datetime
    duration_seconds: int
    location_type: LocationType

    def __post_init__(self) -> None:
        for name in (
            "old_start_utc",
            "old_end_utc",
            "new_start_utc",
            "new_end_utc",
        ):
            require_utc_datetime(getattr(self, name), name)
        require_non_blank(self.timezone, "timezone")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleApplyValidation:
    passed: bool
    violations: tuple[SafetyViolation, ...]
    busy_snapshot_age_seconds: int
    calendar_verification_status: CalendarVerificationStatus
    calendar_revalidated: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleApplyPreview:
    draft_id: UUID
    draft_version: int
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    assignment_changes: tuple[ScheduleAssignmentChange, ...]
    preserved_session_ids: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    validation: ScheduleApplyValidation
    application_fingerprint: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleApplicationMetadata:
    source_schedule_draft_id: UUID
    source_schedule_draft_version: int
    source_context_snapshot_reference_id: UUID
    source_busy_snapshot_id: UUID
    source_candidate_set_id: UUID
    source_revision: int
    created_revision: int
    changed_session_ids: tuple[UUID, ...]
    preserved_session_ids: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    calendar_verification_status: CalendarVerificationStatus
    application_fingerprint: str
    policy_version: str = "schedule-plan-apply-v1"

    def __post_init__(self) -> None:
        require_non_blank(self.application_fingerprint, "application_fingerprint")
        require_non_blank(self.policy_version, "policy_version")
        if self.created_revision != self.source_revision + 1:
            raise ValueError(
                "created_revision must immediately follow source_revision."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleApplicationResult:
    id: UUID
    user_id: UUID
    client_request_id: str
    application_fingerprint: str
    schedule_draft_id: UUID
    root_plan_id: UUID
    source_revision: int
    created_revision: int
    previous_plan_version: int
    resulting_plan_version: int
    changed_session_ids: tuple[UUID, ...]
    calendar_verification_status: CalendarVerificationStatus
    created_at: datetime

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        require_non_blank(self.application_fingerprint, "application_fingerprint")
        require_utc_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleApplicationCommit:
    result: ScheduleApplicationResult
    plan_revision_id: UUID
    draft_id: UUID
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleApplyOutcome:
    result: ScheduleApplicationResult
    plan: WeeklyPlan
    draft: ScheduleDraft
    created: bool
