"""Strict HTTP contracts for review-only Session Design Drafts."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.common import LocationType
from app.domain.profiles.models import FitnessGoal
from app.domain.session_design.enums import (
    SessionDesignDraftStatus,
    SessionDesignSource,
    SessionExerciseRole,
    SessionTemplateId,
)
from app.domain.session_design.models import SessionDesignDraft, SessionDesignTrace
from app.domain.sessions.models import SessionType


class CreateSessionDesignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=120)
    target_date: date
    target_duration_minutes: int = Field(ge=15, le=60)
    location: LocationType
    goal: FitnessGoal
    preferred_session_type: SessionType | None = None
    template_id: SessionTemplateId | None = None


class ReviewSessionDesignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class SessionDesignExerciseResponse(BaseModel):
    exercise_id: str
    sequence_no: int
    role: SessionExerciseRole
    sets: int | None
    repetitions: int | None
    duration_seconds: int | None
    rest_seconds: int


class SessionDesignDraftResponse(BaseModel):
    id: UUID
    request_id: UUID
    client_request_id: str
    status: SessionDesignDraftStatus
    version: int
    source: SessionDesignSource
    template_id: SessionTemplateId
    template_version: str
    catalog_version: str
    session_type: SessionType
    target_date: date
    target_duration_minutes: int
    location: LocationType
    goal: FitnessGoal
    exercises: tuple[SessionDesignExerciseResponse, ...]
    duration_policy_version: str
    total_seconds: int
    safety_passed: bool
    candidate_set_id: UUID
    candidate_set_fingerprint: str
    context_snapshot_reference_id: UUID
    context_fingerprint: str
    prompt_version: str
    fallback_used: bool
    explanation_summary: str
    created_at: datetime
    expires_at: datetime
    reviewed_at: datetime | None
    applied_root_plan_id: UUID | None
    applied_revision: int | None
    applied_session_id: UUID | None
    application_result_id: UUID | None
    applied_at: datetime | None

    @classmethod
    def from_domain(cls, draft: SessionDesignDraft) -> "SessionDesignDraftResponse":
        return cls(
            id=draft.id,
            request_id=draft.request_id,
            client_request_id=draft.client_request_id,
            status=draft.status,
            version=draft.version,
            source=draft.source,
            template_id=draft.template_id,
            template_version=draft.template_version,
            catalog_version=draft.catalog_version,
            session_type=draft.session_type,
            target_date=draft.target_date,
            target_duration_minutes=draft.target_duration_minutes,
            location=draft.location,
            goal=draft.goal,
            exercises=tuple(
                SessionDesignExerciseResponse(
                    exercise_id=item.exercise_id,
                    sequence_no=item.sequence_no,
                    role=role,
                    sets=item.sets,
                    repetitions=item.repetitions,
                    duration_seconds=item.duration_seconds,
                    rest_seconds=item.rest_seconds,
                )
                for item, role in zip(
                    draft.exercises, draft.exercise_roles, strict=True
                )
            ),
            duration_policy_version=draft.duration.policy_version,
            total_seconds=draft.duration.total_seconds,
            safety_passed=draft.safety_validation.passed,
            candidate_set_id=draft.candidate_set_id,
            candidate_set_fingerprint=draft.candidate_set_fingerprint,
            context_snapshot_reference_id=draft.context_snapshot_reference_id,
            context_fingerprint=draft.context_fingerprint,
            prompt_version=draft.prompt_version,
            fallback_used=draft.fallback_used,
            explanation_summary=draft.explanation_summary,
            created_at=draft.created_at,
            expires_at=draft.expires_at,
            reviewed_at=draft.reviewed_at,
            applied_root_plan_id=draft.applied_root_plan_id,
            applied_revision=draft.applied_revision,
            applied_session_id=draft.applied_session_id,
            application_result_id=draft.application_result_id,
            applied_at=draft.applied_at,
        )


class SessionDesignTraceResponse(BaseModel):
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

    @classmethod
    def from_domain(cls, trace: SessionDesignTrace) -> "SessionDesignTraceResponse":
        return cls(
            draft_id=trace.draft_id,
            request_id=trace.request_id,
            candidate_set_id=trace.candidate_set_id,
            candidate_set_fingerprint=trace.candidate_set_fingerprint,
            context_snapshot_reference_id=trace.context_snapshot_reference_id,
            context_fingerprint=trace.context_fingerprint,
            prompt_version=trace.prompt_version,
            template_id=trace.template_id,
            template_version=trace.template_version,
            provider_summary=trace.provider_summary,
            source=trace.source,
            fallback_used=trace.fallback_used,
            validation_error_code=trace.validation_error_code,
            model_trace_ids=trace.model_trace_ids,
        )


class SessionDesignMetricsResponse(BaseModel):
    requests_total: int
    model_drafts: int
    template_fallbacks: int
    validation_failures: int
    idempotent_reuses: int
    accepted: int
    rejected: int
