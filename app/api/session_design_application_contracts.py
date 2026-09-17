"""HTTP contracts for applying one accepted Session Design to a Plan Revision."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.api.contracts import (
    SafetyValidationResponse,
    SessionExerciseResponse,
    WeeklyPlanResponse,
)
from app.api.session_design_contracts import SessionDesignDraftResponse
from app.application.session_design_application import SessionDesignApplyOutcome
from app.domain.session_design_application.models import (
    ApplySessionDesignCommand,
    SessionCompositionSummary,
    SessionDesignApplicationResult,
    SessionDesignApplyPreview,
)
from app.domain.sessions.models import SessionType


class ApplySessionDesignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=128)
    expected_draft_version: int = Field(ge=1)
    root_plan_id: UUID
    source_revision: int = Field(ge=1)
    expected_plan_version: int = Field(ge=1)
    target_session_id: UUID

    def to_command(self) -> ApplySessionDesignCommand:
        return ApplySessionDesignCommand(**self.model_dump())


class SessionCompositionSummaryResponse(BaseModel):
    session_id: UUID
    session_type: SessionType
    session_version: int
    exercise_ids: tuple[str, ...]
    exercises: tuple[SessionExerciseResponse, ...]
    calculated_seconds: int

    @classmethod
    def from_domain(
        cls, value: SessionCompositionSummary
    ) -> "SessionCompositionSummaryResponse":
        return cls(
            session_id=value.session_id,
            session_type=value.session_type,
            session_version=value.session_version,
            exercise_ids=value.exercise_ids,
            exercises=tuple(
                SessionExerciseResponse.from_domain(item) for item in value.exercises
            ),
            calculated_seconds=value.calculated_seconds,
        )


class SessionDesignApplyPreviewResponse(BaseModel):
    draft_id: UUID
    draft_version: int
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    target_session_id: UUID
    target_session_before: SessionCompositionSummaryResponse
    target_session_after: SessionCompositionSummaryResponse
    preserved_session_ids: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    changed_session_ids: tuple[UUID, ...]
    duration_delta_seconds: int
    available_window_seconds: int
    validation: SafetyValidationResponse
    application_fingerprint: str

    @classmethod
    def from_domain(cls, value: SessionDesignApplyPreview) -> Self:
        return cls(
            draft_id=value.draft_id,
            draft_version=value.draft_version,
            root_plan_id=value.root_plan_id,
            source_revision=value.source_revision,
            source_plan_version=value.source_plan_version,
            target_session_id=value.target_session_id,
            target_session_before=SessionCompositionSummaryResponse.from_domain(
                value.target_session_before
            ),
            target_session_after=SessionCompositionSummaryResponse.from_domain(
                value.target_session_after
            ),
            preserved_session_ids=value.preserved_session_ids,
            immutable_session_ids=value.immutable_session_ids,
            changed_session_ids=value.changed_session_ids,
            duration_delta_seconds=value.duration_delta_seconds,
            available_window_seconds=value.available_window_seconds,
            validation=SafetyValidationResponse.from_domain(value.validation),
            application_fingerprint=value.application_fingerprint,
        )


class SessionDesignApplicationResultResponse(BaseModel):
    id: UUID
    user_id: UUID
    client_request_id: str
    application_fingerprint: str
    draft_id: UUID
    root_plan_id: UUID
    source_revision: int
    created_revision: int
    target_session_id: UUID
    previous_plan_version: int
    resulting_plan_version: int
    created_at: datetime

    @classmethod
    def from_domain(cls, value: SessionDesignApplicationResult) -> Self:
        return cls(**{field: getattr(value, field) for field in cls.model_fields})


class SessionDesignApplyResponse(BaseModel):
    result: SessionDesignApplicationResultResponse
    plan: WeeklyPlanResponse
    draft: SessionDesignDraftResponse
    created: bool

    @classmethod
    def from_outcome(cls, value: SessionDesignApplyOutcome) -> Self:
        return cls(
            result=SessionDesignApplicationResultResponse.from_domain(value.result),
            plan=WeeklyPlanResponse.from_domain(value.plan),
            draft=SessionDesignDraftResponse.from_domain(value.draft),
            created=value.created,
        )
