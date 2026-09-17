"""Weekly plan aggregate."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.domain.common import (
    DomainConflictError,
    DomainValidationError,
    InvalidDomainStateTransition,
    require_utc_datetime,
    require_version,
)
from app.domain.recovery_application.models import RecoveryPlanApplicationMetadata
from app.domain.replanning.models import PlanChangeMetadata
from app.domain.schedule_application.models import ScheduleApplicationMetadata
from app.domain.session_design_application.models import (
    SessionDesignApplicationMetadata,
)
from app.domain.sessions.models import WorkoutSession


class WeeklyPlanStatus(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True, kw_only=True)
class WeeklyPlan:
    id: UUID
    user_id: UUID
    week_start: date
    status: WeeklyPlanStatus
    revision: int
    goal_snapshot: dict[str, Any]
    constraint_snapshot: tuple[dict[str, Any], ...]
    estimated_total_minutes: int
    sessions: tuple[WorkoutSession, ...]
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    version: int
    generation_metadata: dict[str, str] | None = None
    root_plan_id: UUID | None = None
    parent_revision: int | None = None
    revision_reason: str | None = None
    change_metadata: (
        PlanChangeMetadata
        | SessionDesignApplicationMetadata
        | ScheduleApplicationMetadata
        | RecoveryPlanApplicationMetadata
        | None
    ) = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.user_id, UUID):
            raise DomainValidationError("id and user_id must be UUID values.")
        if not isinstance(self.week_start, date) or isinstance(
            self.week_start, datetime
        ):
            raise DomainValidationError("week_start must be a date.")
        if not isinstance(self.status, WeeklyPlanStatus):
            raise DomainValidationError("status must be a WeeklyPlanStatus.")
        if self.week_start.weekday() != 0:
            raise DomainValidationError("week_start must be a Monday.")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise DomainValidationError("revision must start at 1.")
        if not isinstance(self.goal_snapshot, dict):
            raise DomainValidationError("goal_snapshot must be a dictionary.")
        if not isinstance(self.constraint_snapshot, tuple) or any(
            not isinstance(item, dict) for item in self.constraint_snapshot
        ):
            raise DomainValidationError(
                "constraint_snapshot must be a tuple of dictionaries."
            )
        if not isinstance(self.sessions, tuple) or any(
            not isinstance(item, WorkoutSession) for item in self.sessions
        ):
            raise DomainValidationError(
                "sessions must be a tuple of WorkoutSession values."
            )
        if self.root_plan_id is not None and not isinstance(self.root_plan_id, UUID):
            raise DomainValidationError("root_plan_id must be a UUID when provided.")
        if any(session.plan_id != self.series_id for session in self.sessions):
            raise DomainValidationError(
                "every session must belong to this plan revision series."
            )
        expected_minutes = sum(session.estimated_minutes for session in self.sessions)
        if isinstance(self.estimated_total_minutes, bool) or not isinstance(
            self.estimated_total_minutes, int
        ):
            raise DomainValidationError("estimated_total_minutes must be an integer.")
        if self.estimated_total_minutes != expected_minutes:
            raise DomainValidationError(
                "estimated_total_minutes must equal the session-minute total."
            )
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise DomainValidationError("updated_at cannot precede created_at.")
        if self.confirmed_at is not None:
            require_utc_datetime(self.confirmed_at, "confirmed_at")
        if self.status is WeeklyPlanStatus.CONFIRMED and self.confirmed_at is None:
            raise DomainValidationError("confirmed plans require confirmed_at.")
        if (
            self.status is not WeeklyPlanStatus.CONFIRMED
            and self.confirmed_at is not None
        ):
            raise DomainValidationError(
                "confirmed_at is only valid for CONFIRMED plans."
            )
        require_version(self.version)
        if self.generation_metadata is not None:
            if not isinstance(self.generation_metadata, dict) or any(
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(value, str)
                or not value.strip()
                for key, value in self.generation_metadata.items()
            ):
                raise DomainValidationError(
                    "generation_metadata must contain non-blank string pairs."
                )
        if self.parent_revision is not None:
            if self.parent_revision < 1 or self.parent_revision >= self.revision:
                raise DomainValidationError(
                    "parent_revision must precede the current revision."
                )
            if self.root_plan_id is None or self.change_metadata is None:
                raise DomainValidationError(
                    "replanned revisions require root_plan_id and change_metadata."
                )
        if self.change_metadata is not None:
            if self.parent_revision != self.change_metadata.source_revision:
                raise DomainValidationError(
                    "change metadata must identify the parent revision."
                )
            if not self.revision_reason:
                raise DomainValidationError(
                    "replanned revisions require a revision_reason."
                )
        # Own snapshot containers so callers cannot mutate through their input values.
        object.__setattr__(self, "goal_snapshot", deepcopy(self.goal_snapshot))
        object.__setattr__(
            self,
            "constraint_snapshot",
            tuple(deepcopy(item) for item in self.constraint_snapshot),
        )
        object.__setattr__(
            self,
            "generation_metadata",
            deepcopy(self.generation_metadata),
        )

    @property
    def series_id(self) -> UUID:
        """Stable logical-plan identifier shared by all revisions."""

        return self.root_plan_id or self.id

    def confirm(self, *, expected_version: int, confirmed_at: datetime) -> WeeklyPlan:
        """Return the confirmed next version of a validated plan."""

        require_version(expected_version)
        require_utc_datetime(confirmed_at, "confirmed_at")
        if expected_version != self.version:
            detail = (
                f"expected version {expected_version}, "
                f"current version is {self.version}."
            )
            raise DomainConflictError(detail)
        if self.status is not WeeklyPlanStatus.VALIDATED:
            raise InvalidDomainStateTransition(
                "only a VALIDATED plan can be confirmed."
            )
        if confirmed_at < self.created_at:
            raise DomainValidationError("confirmed_at cannot precede created_at.")
        return replace(
            self,
            status=WeeklyPlanStatus.CONFIRMED,
            confirmed_at=confirmed_at,
            updated_at=confirmed_at,
            version=self.version + 1,
        )
