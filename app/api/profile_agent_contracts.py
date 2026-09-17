"""Stable Phase 3A Profile Agent, trace, and metrics HTTP contracts."""

from datetime import date, datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.contracts import RequestModel
from app.domain.common import LocationType
from app.domain.context.enums import ContextDegradedMode
from app.domain.model_gateway.enums import (
    FallbackType,
    ModelAttemptOutcome,
    ModelErrorCode,
    ProviderRole,
)
from app.domain.model_gateway.models import ModelCallTrace
from app.domain.orchestration.enums import PlanningRunStatus
from app.domain.profile_agent.apply_models import (
    ApplyValidationResult,
    ConstraintConflict,
    ConstraintPreview,
    FitnessProfilePreview,
    ProfileDraftApplyDecision,
    ProfileDraftApplyPreview,
    ProfileDraftApplyResult,
    ProfileDraftRejectResult,
    ProfileFieldChange,
)
from app.domain.profile_agent.memory_candidates import (
    DraftMemoryCandidateImportResult,
    DraftMemoryCandidatePreview,
)
from app.domain.profile_agent.models import (
    MemoryCandidateProposal,
    PreferenceProposal,
    ProfileAgentDraft,
    ProfileAgentOutput,
    ProfileDraftStatus,
)
from app.domain.profiles.models import ExperienceLevel, FitnessGoal
from app.model_gateway.metrics import ModelGatewayMetricsSnapshot


class ParseProfileRequest(RequestModel):
    client_request_id: str = Field(min_length=1, max_length=120)
    user_message: str = Field(min_length=1, max_length=4000)
    current_week: date

    @field_validator("client_request_id", "user_message")
    @classmethod
    def strip_non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("current_week")
    @classmethod
    def require_monday(cls, value: date) -> date:
        if value.weekday() != 0:
            raise ValueError("current_week must be a Monday")
        return value


class CreateProfileAgentRunRequest(ParseProfileRequest):
    pass


class ProfileAgentRunCreateResponse(BaseModel):
    run_id: UUID
    status: PlanningRunStatus
    status_url: str


class ProfileAgentDraftResponse(BaseModel):
    id: UUID
    request_id: UUID
    output: ProfileAgentOutput
    prompt_version: str
    provider_summary: str
    fallback_used: bool
    fallback_type: FallbackType | None
    created_at: datetime
    expires_at: datetime
    status: ProfileDraftStatus
    version: int
    applied_at: datetime | None
    rejected_at: datetime | None
    applied_profile_id: UUID | None
    context_snapshot_reference_id: UUID | None
    context_fingerprint: str | None
    context_contract_version: str | None
    context_policy_version: str | None
    context_degraded_mode: ContextDegradedMode
    context_included_memory_count: int

    @classmethod
    def from_domain(cls, draft: ProfileAgentDraft) -> Self:
        return cls(
            id=draft.id,
            request_id=draft.request_id,
            output=draft.output,
            prompt_version=draft.prompt_version,
            provider_summary=draft.provider_summary,
            fallback_used=draft.fallback_used,
            fallback_type=draft.fallback_type,
            created_at=draft.created_at,
            expires_at=draft.expires_at,
            status=draft.status,
            version=draft.version,
            applied_at=draft.applied_at,
            rejected_at=draft.rejected_at,
            applied_profile_id=draft.applied_profile_id,
            context_snapshot_reference_id=draft.context_snapshot_reference_id,
            context_fingerprint=draft.context_fingerprint,
            context_contract_version=draft.context_contract_version,
            context_policy_version=draft.context_policy_version,
            context_degraded_mode=draft.context_degraded_mode,
            context_included_memory_count=draft.context_included_memory_count,
        )


class ProfileDraftApplyDecisionRequest(RequestModel):
    client_request_id: str = Field(min_length=1, max_length=120)
    expected_draft_version: int = Field(ge=1)
    expected_profile_version: int | None = Field(default=None, ge=1)
    accept_weekly_frequency: bool = False
    accept_max_session_minutes: bool = False
    selected_primary_goal: FitnessGoal | None = None
    accepted_equipment: tuple[str, ...] = ()
    accepted_locations: tuple[LocationType, ...] = ()
    accepted_hard_constraint_indexes: tuple[int, ...] = ()
    accepted_temporary_constraint_indexes: tuple[int, ...] = ()
    temporary_constraint_expirations: dict[int, datetime] = Field(default_factory=dict)
    confirmed_experience_level: ExperienceLevel | None = None
    confirm_scope: bool = False

    @field_validator("client_request_id")
    @classmethod
    def normalize_request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("client_request_id must not be blank")
        return normalized

    @field_validator("accepted_equipment")
    @classmethod
    def normalize_equipment(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized) or len(set(normalized)) != len(
            normalized
        ):
            raise ValueError("accepted_equipment must contain unique nonblank values")
        return normalized

    @field_validator(
        "accepted_locations",
        "accepted_hard_constraint_indexes",
        "accepted_temporary_constraint_indexes",
    )
    @classmethod
    def require_unique(cls, values: tuple[object, ...]) -> tuple[object, ...]:
        if len(set(values)) != len(values):
            raise ValueError("accepted values must be unique")
        return values

    def to_domain(self) -> ProfileDraftApplyDecision:
        return ProfileDraftApplyDecision(
            client_request_id=self.client_request_id,
            expected_draft_version=self.expected_draft_version,
            expected_profile_version=self.expected_profile_version,
            accept_weekly_frequency=self.accept_weekly_frequency,
            accept_max_session_minutes=self.accept_max_session_minutes,
            selected_primary_goal=self.selected_primary_goal,
            accepted_equipment=self.accepted_equipment,
            accepted_locations=self.accepted_locations,
            accepted_hard_constraint_indexes=self.accepted_hard_constraint_indexes,
            accepted_temporary_constraint_indexes=(
                self.accepted_temporary_constraint_indexes
            ),
            temporary_constraint_expirations=self.temporary_constraint_expirations,
            confirmed_experience_level=self.confirmed_experience_level,
            confirm_scope=self.confirm_scope,
        )


class ProfileDraftApplyPreviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    draft_id: UUID
    draft_version: int
    expires_at: datetime
    context_snapshot_reference_id: UUID | None
    context_fingerprint: str | None
    context_contract_version: str | None
    context_policy_version: str | None
    context_degraded_mode: ContextDegradedMode
    current_profile_version: int | None
    profile_changes: tuple[ProfileFieldChange, ...]
    constraints_to_add: tuple[ConstraintPreview, ...]
    constraints_unchanged: tuple[ConstraintPreview, ...]
    constraints_conflicting: tuple[ConstraintConflict, ...]
    ignored_soft_preferences: tuple[PreferenceProposal, ...]
    ignored_memory_candidates: tuple[MemoryCandidateProposal, ...]
    result_profile: FitnessProfilePreview
    validation: ApplyValidationResult
    apply_fingerprint: str
    policy_version: str

    @classmethod
    def from_domain(cls, value: ProfileDraftApplyPreview) -> Self:
        return cls.model_validate(value)


class ProfileDraftApplyResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    draft_id: UUID
    profile_id: UUID
    previous_profile_version: int | None
    resulting_profile_version: int
    added_constraint_ids: tuple[UUID, ...]
    unchanged_constraint_ids: tuple[UUID, ...]
    ignored_soft_preference_count: int
    ignored_memory_candidate_count: int
    applied_at: datetime

    @classmethod
    def from_domain(cls, value: ProfileDraftApplyResult) -> Self:
        return cls.model_validate(value)


class RejectProfileDraftRequest(RequestModel):
    client_request_id: str = Field(min_length=1, max_length=120)
    expected_draft_version: int = Field(ge=1)


class ProfileDraftRejectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    draft_id: UUID
    client_request_id: str
    rejected_at: datetime
    created: bool

    @classmethod
    def from_domain(cls, value: ProfileDraftRejectResult) -> Self:
        return cls.model_validate(value)


class PreviewDraftMemoryCandidatesRequest(RequestModel):
    selected_candidate_indexes: tuple[int, ...] = Field(min_length=1)


class ImportDraftMemoryCandidatesRequest(PreviewDraftMemoryCandidatesRequest):
    client_request_id: str = Field(min_length=1, max_length=120)
    expected_draft_version: int = Field(ge=1)


class DraftMemoryCandidatePreviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    draft_id: UUID
    draft_version: int
    items: tuple["DraftMemoryCandidatePreviewItemResponse", ...]

    @classmethod
    def from_domain(cls, value: DraftMemoryCandidatePreview) -> Self:
        return cls.model_validate(value)


class DraftMemoryCandidatePreviewItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    proposal_index: int
    memory_type: str | None
    proposed_key: str | None
    proposed_value: str
    source_reference: str
    evidence_summary: str
    supported: bool
    conflict: bool
    duplicate: bool
    rejection_reason: str | None


class DraftMemoryCandidateImportResponse(BaseModel):
    draft_id: UUID
    client_request_id: str
    import_fingerprint: str
    candidate_ids: tuple[UUID, ...]
    statuses: tuple[str, ...]
    created: bool

    @classmethod
    def from_domain(cls, value: DraftMemoryCandidateImportResult) -> Self:
        return cls(
            draft_id=value.draft_id,
            client_request_id=value.client_request_id,
            import_fingerprint=value.import_fingerprint,
            candidate_ids=value.candidate_ids,
            statuses=tuple(item.value for item in value.statuses),
            created=value.created,
        )


class ModelCallTraceResponse(BaseModel):
    id: UUID
    request_id: UUID
    agent_name: str
    prompt_name: str
    prompt_version: str
    prompt_sha256: str
    provider_name: str
    provider_version: str
    provider_role: ProviderRole
    model: str
    attempt_no: int
    outcome: ModelAttemptOutcome
    error_code: ModelErrorCode | None
    error_message: str | None
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    fallback_used: bool
    fallback_type: FallbackType | None
    input_fingerprint: str
    scope_guard_blocked: bool
    created_at: datetime
    context_snapshot_reference_id: UUID | None
    context_fingerprint: str | None
    context_contract_version: str | None
    context_degraded_mode: ContextDegradedMode

    @classmethod
    def from_domain(cls, trace: ModelCallTrace) -> Self:
        return cls(**{field: getattr(trace, field) for field in cls.model_fields})


class ModelGatewayMetricsResponse(BaseModel):
    requests_total: int
    provider_attempts_total: int
    primary_successes: int
    backup_successes: int
    template_fallbacks: int
    timeouts: int
    rate_limited: int
    server_errors: int
    authentication_errors: int
    invalid_json: int
    schema_failures: int
    business_validation_failures: int
    scope_guard_blocks: int
    requests_in_flight: int

    @classmethod
    def from_domain(cls, value: ModelGatewayMetricsSnapshot) -> Self:
        return cls(**value.as_dict())
