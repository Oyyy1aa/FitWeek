"""Immutable, non-medical workout check-in facts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.common import (
    DomainValidationError,
    require_non_blank,
    require_utc_datetime,
    require_version,
)


class CheckInStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkoutCheckIn:
    """A user-reported execution fact; it carries no medical interpretation."""

    id: UUID
    client_event_id: str
    user_id: UUID
    plan_id: UUID
    plan_revision: int
    session_id: UUID
    status: CheckInStatus
    actual_minutes: int | None
    perceived_effort: int | None
    note: str | None
    occurred_at: datetime
    created_at: datetime
    updated_at: datetime
    version: int

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, UUID)
            for value in (self.id, self.user_id, self.plan_id, self.session_id)
        ):
            raise DomainValidationError("check-in identifiers must be UUID values.")
        require_non_blank(self.client_event_id, "client_event_id")
        if len(self.client_event_id.strip()) > 128:
            raise DomainValidationError(
                "client_event_id must not exceed 128 characters."
            )
        if not isinstance(self.status, CheckInStatus):
            raise DomainValidationError("status must be a CheckInStatus.")
        if (
            isinstance(self.plan_revision, bool)
            or not isinstance(self.plan_revision, int)
            or self.plan_revision < 1
        ):
            raise DomainValidationError("plan_revision must be a positive integer.")
        if self.actual_minutes is not None and (
            isinstance(self.actual_minutes, bool)
            or not isinstance(self.actual_minutes, int)
            or not 0 <= self.actual_minutes <= 120
        ):
            raise DomainValidationError("actual_minutes must be between 0 and 120.")
        if self.status in {
            CheckInStatus.COMPLETED,
            CheckInStatus.PARTIALLY_COMPLETED,
        } and (self.actual_minutes is None or self.actual_minutes <= 0):
            raise DomainValidationError(
                "completed and partially completed check-ins require actual_minutes."
            )
        if self.status is CheckInStatus.SKIPPED and self.actual_minutes not in {
            None,
            0,
        }:
            raise DomainValidationError(
                "skipped check-ins must omit actual_minutes or set it to zero."
            )
        if self.perceived_effort is not None and (
            isinstance(self.perceived_effort, bool)
            or not isinstance(self.perceived_effort, int)
            or not 1 <= self.perceived_effort <= 10
        ):
            raise DomainValidationError(
                "perceived_effort must be between 1 and 10 when provided."
            )
        if self.note is not None and len(self.note) > 500:
            raise DomainValidationError("note must not exceed 500 characters.")
        require_utc_datetime(self.occurred_at, "occurred_at")
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise DomainValidationError("updated_at cannot precede created_at.")
        require_version(self.version)
        object.__setattr__(self, "client_event_id", self.client_event_id.strip())

    def same_event_payload(self, other: "WorkoutCheckIn") -> bool:
        """Compare the immutable client-supplied portion of an idempotent event."""

        return (
            self.session_id,
            self.status,
            self.actual_minutes,
            self.perceived_effort,
            self.occurred_at,
            self.note,
        ) == (
            other.session_id,
            other.status,
            other.actual_minutes,
            other.perceived_effort,
            other.occurred_at,
            other.note,
        )
