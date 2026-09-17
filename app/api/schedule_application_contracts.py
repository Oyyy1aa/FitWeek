"""HTTP contracts for Schedule Draft Plan application."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.api.contracts import SafetyViolationResponse, WeeklyPlanResponse
from app.domain.schedule_application.models import (
    ApplyScheduleDraftCommand,
    ScheduleApplicationResult,
    ScheduleApplyOutcome,
    ScheduleApplyPreview,
    ScheduleAssignmentChange,
)
from app.domain.scheduling.enums import CalendarVerificationStatus


class ApplyScheduleDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=128)
    expected_draft_version: int = Field(ge=1)
    root_plan_id: UUID
    source_revision: int = Field(ge=1)
    expected_plan_version: int = Field(ge=1)

    def to_command(self) -> ApplyScheduleDraftCommand:
        return ApplyScheduleDraftCommand(**self.model_dump())


class ScheduleAssignmentChangeResponse(BaseModel):
    session_id: UUID
    old_start_utc: datetime
    old_end_utc: datetime
    new_start_utc: datetime
    new_end_utc: datetime
    timezone: str
    old_start_local: datetime
    new_start_local: datetime
    duration_seconds: int
    location_type: str

    @classmethod
    def from_domain(cls, value: ScheduleAssignmentChange) -> Self:
        return cls(**{name: getattr(value, name) for name in cls.model_fields})


class ScheduleApplyPreviewResponse(BaseModel):
    draft_id: UUID
    draft_version: int
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    assignment_changes: tuple[ScheduleAssignmentChangeResponse, ...]
    preserved_session_ids: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    passed: bool
    violations: tuple[SafetyViolationResponse, ...]
    busy_snapshot_age_seconds: int
    calendar_verification_status: CalendarVerificationStatus
    calendar_revalidated: bool
    application_fingerprint: str

    @classmethod
    def from_domain(cls, value: ScheduleApplyPreview) -> Self:
        return cls(
            draft_id=value.draft_id,
            draft_version=value.draft_version,
            root_plan_id=value.root_plan_id,
            source_revision=value.source_revision,
            source_plan_version=value.source_plan_version,
            assignment_changes=tuple(
                ScheduleAssignmentChangeResponse.from_domain(item)
                for item in value.assignment_changes
            ),
            preserved_session_ids=value.preserved_session_ids,
            immutable_session_ids=value.immutable_session_ids,
            passed=value.validation.passed,
            violations=tuple(
                SafetyViolationResponse.from_domain(item)
                for item in value.validation.violations
            ),
            busy_snapshot_age_seconds=value.validation.busy_snapshot_age_seconds,
            calendar_verification_status=(
                value.validation.calendar_verification_status
            ),
            calendar_revalidated=value.validation.calendar_revalidated,
            application_fingerprint=value.application_fingerprint,
        )


class ScheduleApplicationResultResponse(BaseModel):
    id: UUID
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

    @classmethod
    def from_domain(cls, value: ScheduleApplicationResult) -> Self:
        return cls(**{name: getattr(value, name) for name in cls.model_fields})


class ScheduleApplyResponse(BaseModel):
    result: ScheduleApplicationResultResponse
    plan: WeeklyPlanResponse
    created: bool

    @classmethod
    def from_outcome(cls, value: ScheduleApplyOutcome) -> Self:
        return cls(
            result=ScheduleApplicationResultResponse.from_domain(value.result),
            plan=WeeklyPlanResponse.from_domain(value.plan),
            created=value.created,
        )
