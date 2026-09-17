"""Immutable Session Designer request, candidate, output, and Draft values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.common import (
    DomainValidationError,
    LocationType,
    require_non_blank,
    require_utc_datetime,
    require_version,
)
from app.domain.context.enums import ContextDegradedMode
from app.domain.profiles.models import FitnessGoal
from app.domain.session_design.enums import (
    SessionDesignDraftStatus,
    SessionDesignSource,
    SessionExerciseRole,
    SessionTemplateId,
)
from app.domain.sessions.models import SessionExercise, SessionType
from app.safety.models import SafetyValidationResult


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDesignRequest:
    client_request_id: str
    target_date: date
    target_duration_minutes: int
    location: LocationType
    goal: FitnessGoal
    preferred_session_type: SessionType | None = None
    template_id: SessionTemplateId | None = None

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        if not 15 <= self.target_duration_minutes <= 60:
            raise DomainValidationError("target duration must be between 15 and 60.")


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateSlot:
    slot_id: str
    role: SessionExerciseRole
    exercise_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_non_blank(self.slot_id, "slot_id")
        if not self.exercise_ids:
            raise DomainValidationError("candidate slot cannot be empty.")
        if len(self.exercise_ids) != len(set(self.exercise_ids)):
            raise DomainValidationError("candidate exercise IDs must be unique.")
        if self.exercise_ids != tuple(sorted(self.exercise_ids)):
            raise DomainValidationError("candidate exercise IDs must be sorted.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExerciseCandidateSet:
    id: UUID
    user_id: UUID
    request_fingerprint: str
    fingerprint: str
    template_id: SessionTemplateId
    template_version: str
    catalog_version: str
    context_snapshot_reference_id: UUID
    context_fingerprint: str
    slots: tuple[CandidateSlot, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        require_non_blank(self.request_fingerprint, "request_fingerprint")
        require_non_blank(self.fingerprint, "fingerprint")
        require_non_blank(self.catalog_version, "catalog_version")
        require_non_blank(self.context_fingerprint, "context_fingerprint")
        require_utc_datetime(self.created_at, "created_at")
        ids = tuple(item.slot_id for item in self.slots)
        if len(ids) != len(set(ids)) or not ids:
            raise DomainValidationError("candidate slots must be non-empty and unique.")

    def allowed(self, slot_id: str, exercise_id: str) -> bool:
        return any(
            item.slot_id == slot_id and exercise_id in item.exercise_ids
            for item in self.slots
        )


class SessionDesignerSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str = Field(min_length=1, max_length=80)
    exercise_id: str = Field(min_length=1, max_length=100)
    sets: int | None = Field(default=None, ge=1, le=5)
    repetitions: int | None = Field(default=None, ge=1, le=30)
    duration_seconds: int | None = Field(default=None, ge=1, le=1800)
    rest_seconds: int = Field(default=0, ge=0, le=180)

    @field_validator("slot_id", "exercise_id")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()


class SessionDesignerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    template_id: SessionTemplateId
    session_type: SessionType
    selections: Annotated[
        tuple[SessionDesignerSelection, ...], Field(min_length=3, max_length=6)
    ]
    explanation_summary: str = Field(min_length=1, max_length=600)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDurationBreakdown:
    exercise_seconds: int
    rest_seconds: int
    transition_seconds: int
    total_seconds: int
    policy_version: str = "session-duration-policy-v1"

    def __post_init__(self) -> None:
        if min(self.exercise_seconds, self.rest_seconds, self.transition_seconds) < 0:
            raise DomainValidationError("duration components cannot be negative.")
        if self.total_seconds != (
            self.exercise_seconds + self.rest_seconds + self.transition_seconds
        ):
            raise DomainValidationError("duration total must equal its components.")


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDesignDraft:
    id: UUID
    request_id: UUID
    client_request_id: str
    user_id: UUID
    request_payload_fingerprint: str
    candidate_set_id: UUID
    candidate_set_fingerprint: str
    context_snapshot_reference_id: UUID
    context_fingerprint: str
    context_degraded_mode: ContextDegradedMode
    template_id: SessionTemplateId
    template_version: str
    catalog_version: str
    session_type: SessionType
    target_date: date
    target_duration_minutes: int
    location: LocationType
    goal: FitnessGoal
    exercises: tuple[SessionExercise, ...]
    exercise_roles: tuple[SessionExerciseRole, ...]
    duration: SessionDurationBreakdown
    safety_validation: SafetyValidationResult
    source: SessionDesignSource
    prompt_version: str
    provider_summary: str
    fallback_used: bool
    explanation_summary: str
    created_at: datetime
    expires_at: datetime
    status: SessionDesignDraftStatus = SessionDesignDraftStatus.PENDING_REVIEW
    version: int = 1
    reviewed_at: datetime | None = None
    applied_root_plan_id: UUID | None = None
    applied_revision: int | None = None
    applied_session_id: UUID | None = None
    application_result_id: UUID | None = None
    applied_at: datetime | None = None

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        require_non_blank(
            self.request_payload_fingerprint, "request_payload_fingerprint"
        )
        require_non_blank(self.candidate_set_fingerprint, "candidate_set_fingerprint")
        require_non_blank(self.context_fingerprint, "context_fingerprint")
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.expires_at, "expires_at")
        require_version(self.version)
        if self.expires_at <= self.created_at:
            raise DomainValidationError("expires_at must be later than created_at.")
        if len(self.exercises) != len(self.exercise_roles) or not self.exercises:
            raise DomainValidationError("each exercise must have one template role.")
        if self.duration.total_seconds != self.target_duration_minutes * 60:
            raise DomainValidationError("draft duration must exactly match the target.")
        if not self.safety_validation.passed:
            raise DomainValidationError("Draft safety_validation must pass.")
        if self.status is SessionDesignDraftStatus.PENDING_REVIEW:
            if self.reviewed_at is not None:
                raise DomainValidationError("pending Draft cannot have reviewed_at.")
        elif self.reviewed_at is None:
            raise DomainValidationError("terminal Draft must have reviewed_at.")
        applied_values = (
            self.applied_root_plan_id,
            self.applied_revision,
            self.applied_session_id,
            self.application_result_id,
            self.applied_at,
        )
        if self.status is SessionDesignDraftStatus.APPLIED:
            if any(item is None for item in applied_values):
                raise DomainValidationError(
                    "APPLIED Draft requires application audit fields."
                )
            require_utc_datetime(self.applied_at, "applied_at")  # type: ignore[arg-type]
            if self.applied_revision is None or self.applied_revision < 2:
                raise DomainValidationError(
                    "applied_revision must identify a new revision."
                )
        elif any(item is not None for item in applied_values):
            raise DomainValidationError(
                "only an APPLIED Draft has application audit fields."
            )

    def accept(self, at: datetime) -> SessionDesignDraft:
        return self._review(SessionDesignDraftStatus.ACCEPTED, at)

    def reject(self, at: datetime) -> SessionDesignDraft:
        return self._review(SessionDesignDraftStatus.REJECTED, at)

    def expire(self, at: datetime) -> SessionDesignDraft:
        return self._review(SessionDesignDraftStatus.EXPIRED, at)

    def mark_applied(
        self,
        *,
        root_plan_id: UUID,
        revision: int,
        session_id: UUID,
        application_result_id: UUID,
        at: datetime,
    ) -> SessionDesignDraft:
        require_utc_datetime(at, "applied_at")
        if self.status is not SessionDesignDraftStatus.ACCEPTED:
            raise DomainValidationError("only an ACCEPTED Draft can be applied.")
        return replace(
            self,
            status=SessionDesignDraftStatus.APPLIED,
            applied_root_plan_id=root_plan_id,
            applied_revision=revision,
            applied_session_id=session_id,
            application_result_id=application_result_id,
            applied_at=at,
            version=self.version + 1,
        )

    def _review(
        self, status: SessionDesignDraftStatus, at: datetime
    ) -> SessionDesignDraft:
        require_utc_datetime(at, "reviewed_at")
        if self.status is not SessionDesignDraftStatus.PENDING_REVIEW:
            raise DomainValidationError("only a pending Session Draft can be reviewed.")
        return replace(self, status=status, reviewed_at=at, version=self.version + 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDesignTrace:
    draft_id: UUID
    request_id: UUID
    candidate_set_id: UUID
    candidate_set_fingerprint: str
    context_snapshot_reference_id: UUID
    context_fingerprint: str
    prompt_version: str
    template_id: SessionTemplateId
    template_version: str
    provider_summary: str
    source: SessionDesignSource
    fallback_used: bool
    validation_error_code: str | None
    model_trace_ids: tuple[UUID, ...]
