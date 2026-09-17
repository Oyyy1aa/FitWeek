"""Immutable values produced by deterministic behavior analysis."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from app.domain.behavior.enums import (
    BehaviorConfidenceTier,
    BehaviorMemoryProposalStatus,
    BehaviorPatternType,
)
from app.domain.checkins.models import CheckInStatus
from app.domain.common import (
    DomainValidationError,
    require_non_blank,
    require_utc_datetime,
)
from app.domain.memory.enums import MemoryType


@dataclass(frozen=True, slots=True, kw_only=True)
class BehaviorSummaryWindow:
    start_date: date | None = None
    end_date: date | None = None

    def __post_init__(self) -> None:
        if self.start_date is not None and self.end_date is not None:
            if self.start_date >= self.end_date:
                raise DomainValidationError(
                    "behavior window start_date must precede end_date.",
                    code="BEHAVIOR_WINDOW_INVALID",
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class BehaviorEvidenceReference:
    checkin_id: UUID
    logical_session_id: UUID
    root_plan_id: UUID
    plan_revision: int
    scheduled_at_utc: datetime
    status: CheckInStatus
    reported_rpe: int | None
    occurred_at: datetime
    scheduled_weekday: str
    scheduled_time_bucket: str
    location: str
    fingerprint: str

    def __post_init__(self) -> None:
        require_utc_datetime(self.scheduled_at_utc, "scheduled_at_utc")
        require_utc_datetime(self.occurred_at, "occurred_at")
        require_non_blank(self.scheduled_weekday, "scheduled_weekday")
        require_non_blank(self.scheduled_time_bucket, "scheduled_time_bucket")
        require_non_blank(self.location, "location")
        require_non_blank(self.fingerprint, "fingerprint")
        if self.plan_revision < 1:
            raise DomainValidationError("plan_revision must be positive.")
        if self.reported_rpe is not None and not 1 <= self.reported_rpe <= 10:
            raise DomainValidationError("reported_rpe must be between 1 and 10.")


@dataclass(frozen=True, slots=True, kw_only=True)
class BehaviorPattern:
    pattern_id: str
    pattern_type: BehaviorPatternType
    key: str
    occurrence_count: int
    opportunity_count: int
    ratio: Decimal
    evidence_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        require_non_blank(self.pattern_id, "pattern_id")
        require_non_blank(self.key, "key")
        if self.occurrence_count < 1 or self.opportunity_count < 1:
            raise DomainValidationError("pattern counts must be positive.")
        if self.occurrence_count > self.opportunity_count:
            raise DomainValidationError(
                "occurrence count cannot exceed opportunity count."
            )
        if not Decimal("0") <= self.ratio <= Decimal("1"):
            raise DomainValidationError("pattern ratio must be between zero and one.")
        if tuple(sorted(self.evidence_ids, key=str)) != self.evidence_ids:
            raise DomainValidationError("pattern evidence IDs must be stably sorted.")


@dataclass(frozen=True, slots=True, kw_only=True)
class BehaviorSummary:
    id: UUID
    user_id: UUID
    window_start_utc: datetime
    window_end_utc: datetime
    timezone: str
    scheduled_session_count: int
    checked_in_session_count: int
    completed_count: int
    partially_completed_count: int
    skipped_count: int
    missing_checkin_count: int
    completion_rate: Decimal | None
    participation_rate: Decimal | None
    rpe_sample_count: int
    average_reported_rpe: Decimal | None
    high_reported_rpe_count: int
    repeated_time_patterns: tuple[BehaviorPattern, ...]
    repeated_location_patterns: tuple[BehaviorPattern, ...]
    repeated_skip_patterns: tuple[BehaviorPattern, ...]
    evidence_references: tuple[BehaviorEvidenceReference, ...]
    conflict_checkin_ids: tuple[UUID, ...]
    policy_version: str
    fingerprint: str
    created_at: datetime

    def __post_init__(self) -> None:
        require_utc_datetime(self.window_start_utc, "window_start_utc")
        require_utc_datetime(self.window_end_utc, "window_end_utc")
        require_utc_datetime(self.created_at, "created_at")
        require_non_blank(self.timezone, "timezone")
        require_non_blank(self.policy_version, "policy_version")
        require_non_blank(self.fingerprint, "fingerprint")
        if self.window_start_utc >= self.window_end_utc:
            raise DomainValidationError("behavior summary window must be positive.")
        counts = (
            self.scheduled_session_count,
            self.checked_in_session_count,
            self.completed_count,
            self.partially_completed_count,
            self.skipped_count,
            self.missing_checkin_count,
            self.rpe_sample_count,
            self.high_reported_rpe_count,
        )
        if any(value < 0 for value in counts):
            raise DomainValidationError("behavior summary counts cannot be negative.")
        if self.checked_in_session_count != (
            self.completed_count + self.partially_completed_count + self.skipped_count
        ):
            raise DomainValidationError("check-in status counts must reconcile.")
        if self.missing_checkin_count != (
            self.scheduled_session_count - self.checked_in_session_count
        ):
            raise DomainValidationError("missing check-in count must reconcile.")


@dataclass(frozen=True, slots=True, kw_only=True)
class BehaviorMemoryProposal:
    id: UUID
    user_id: UUID
    memory_type: MemoryType
    proposed_key: str
    proposed_value: str
    behavior_pattern_ids: tuple[str, ...]
    evidence_checkin_ids: tuple[UUID, ...]
    confidence_tier: BehaviorConfidenceTier
    status: BehaviorMemoryProposalStatus
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.memory_type not in {
            MemoryType.PREFERRED_TIME_OF_DAY,
            MemoryType.PREFERRED_LOCATION,
        }:
            raise DomainValidationError("behavior proposal memory type is not allowed.")
        require_non_blank(self.proposed_key, "proposed_key")
        require_non_blank(self.proposed_value, "proposed_value")
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise DomainValidationError("behavior proposal must expire in the future.")
        if not self.behavior_pattern_ids or not self.evidence_checkin_ids:
            raise DomainValidationError("behavior proposal requires pattern evidence.")
