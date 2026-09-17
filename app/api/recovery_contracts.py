"""Strict public DTOs for Phase 7A read-only Recovery Drafts."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.contracts import WeeklyPlanResponse
from app.domain.behavior.enums import (
    BehaviorConfidenceTier,
    BehaviorMemoryProposalStatus,
    BehaviorPatternType,
)
from app.domain.behavior.models import (
    BehaviorMemoryProposal,
    BehaviorSummary,
    BehaviorSummaryWindow,
)
from app.domain.checkins.models import CheckInStatus
from app.domain.memory.enums import MemoryType
from app.domain.recovery.enums import (
    RecoveryActionType,
    RecoveryDraftOutcome,
    RecoveryDraftSource,
    RecoveryDraftStatus,
    RecoveryRedesignGoal,
    RecoveryRequestType,
    RecoveryScopeStatus,
)
from app.domain.recovery.models import (
    CreateRecoveryDraftCommand,
    RecoveryActionCandidateSet,
    RecoveryChangeImpactSnapshot,
    RecoveryDraft,
    RecoveryTrace,
)
from app.domain.recovery_application.models import (
    ApplyRecoveryDraftCommand,
    ImportRecoveryMemoryProposalsCommand,
    RecoveryApplicationResult,
    RecoveryApplyPreview,
    RecoveryMemoryProposalImportResult,
    RecoveryMemoryProposalPreview,
)


class BehaviorSummaryWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "BehaviorSummaryWindowRequest":
        if self.start_date is not None and self.end_date is not None:
            if self.start_date >= self.end_date:
                raise ValueError("start_date must precede the exclusive end_date")
            if (self.end_date - self.start_date).days > 56:
                raise ValueError("behavior window must not exceed 56 days")
        return self

    def to_domain(self) -> BehaviorSummaryWindow:
        return BehaviorSummaryWindow(start_date=self.start_date, end_date=self.end_date)


class CreateRecoveryDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=128)
    root_plan_id: UUID
    source_revision: int = Field(ge=1)
    expected_plan_version: int = Field(ge=1)
    request_type: RecoveryRequestType
    target_session_ids: tuple[UUID, ...] | None = Field(default=None, max_length=16)
    user_request: str = Field(min_length=1, max_length=1000)
    behavior_window: BehaviorSummaryWindowRequest | None = None

    @field_validator("client_request_id", "user_request")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    def to_domain(self) -> CreateRecoveryDraftCommand:
        return CreateRecoveryDraftCommand(
            client_request_id=self.client_request_id,
            root_plan_id=self.root_plan_id,
            source_revision=self.source_revision,
            expected_plan_version=self.expected_plan_version,
            request_type=self.request_type,
            target_session_ids=self.target_session_ids,
            user_request=self.user_request,
            behavior_window=(
                self.behavior_window.to_domain() if self.behavior_window else None
            ),
        )


class ReviewRecoveryDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class RecoveryDraftResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    request_id: UUID
    client_request_id: str
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    behavior_summary_id: UUID
    context_snapshot_reference_id: UUID
    change_impact_snapshot_id: UUID
    candidate_set_id: UUID
    selected_action_candidate_ids: tuple[UUID, ...]
    unresolved_session_ids: tuple[UUID, ...]
    behavior_memory_proposal_ids: tuple[UUID, ...]
    explanation_summary: str
    outcome: RecoveryDraftOutcome
    source: RecoveryDraftSource
    fallback_used: bool
    scope_status: RecoveryScopeStatus
    status: RecoveryDraftStatus
    created_at: datetime
    expires_at: datetime
    version: int
    reviewed_at: datetime | None
    application_result_id: UUID | None
    applied_root_plan_id: UUID | None
    applied_source_revision: int | None
    applied_created_revision: int | None
    applied_session_ids: tuple[UUID, ...]
    created_session_design_draft_ids: tuple[UUID, ...]
    created_schedule_draft_ids: tuple[UUID, ...]
    applied_at: datetime | None

    @classmethod
    def from_domain(cls, value: RecoveryDraft) -> "RecoveryDraftResponse":
        return cls.model_validate(value)


class BehaviorPatternResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    pattern_id: str
    pattern_type: BehaviorPatternType
    key: str
    occurrence_count: int
    opportunity_count: int
    ratio: Decimal
    evidence_ids: tuple[UUID, ...]


class BehaviorEvidenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
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


class BehaviorSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
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
    repeated_time_patterns: tuple[BehaviorPatternResponse, ...]
    repeated_location_patterns: tuple[BehaviorPatternResponse, ...]
    repeated_skip_patterns: tuple[BehaviorPatternResponse, ...]
    evidence_references: tuple[BehaviorEvidenceResponse, ...]
    conflict_checkin_ids: tuple[UUID, ...]
    policy_version: str
    fingerprint: str
    created_at: datetime

    @classmethod
    def from_domain(cls, value: BehaviorSummary) -> "BehaviorSummaryResponse":
        return cls.model_validate(value)


class RecoveryChangeImpactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    mutable_session_ids: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    preserved_session_ids: tuple[UUID, ...]
    calendar_bound_session_ids: tuple[UUID, ...]
    completed_checkin_ids: tuple[UUID, ...]
    weekly_frequency_before: int
    minimum_allowed_frequency: int
    maximum_allowed_frequency: int
    requires_session_redesign: bool
    requires_schedule_redraft: bool
    requires_calendar_reconciliation: bool
    requires_new_plan_revision: bool
    fingerprint: str
    created_at: datetime

    @classmethod
    def from_domain(
        cls, value: RecoveryChangeImpactSnapshot
    ) -> "RecoveryChangeImpactResponse":
        return cls.model_validate(value)


class RecoveryActionCandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    action_type: RecoveryActionType
    target_session_id: UUID | None
    target_week_start: date | None
    redesign_goal: RecoveryRedesignGoal | None
    evidence_pattern_ids: tuple[str, ...]
    impact_snapshot_id: UUID
    requires_schedule_draft: bool
    requires_session_design_draft: bool
    requires_plan_revision: bool
    requires_calendar_reconciliation: bool
    deterministic_rank: int


class RecoveryCandidateSetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    behavior_summary_id: UUID
    behavior_summary_fingerprint: str
    context_snapshot_reference_id: UUID
    context_fingerprint: str
    change_impact_snapshot_id: UUID
    candidates: tuple[RecoveryActionCandidateResponse, ...]
    fingerprint: str
    policy_version: str
    prompt_version: str
    created_at: datetime

    @classmethod
    def from_domain(
        cls, value: RecoveryActionCandidateSet
    ) -> "RecoveryCandidateSetResponse":
        return cls.model_validate(value)


class BehaviorMemoryProposalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    memory_type: MemoryType
    proposed_key: str
    proposed_value: str
    behavior_pattern_ids: tuple[str, ...]
    evidence_checkin_ids: tuple[UUID, ...]
    confidence_tier: BehaviorConfidenceTier
    status: BehaviorMemoryProposalStatus
    created_at: datetime
    expires_at: datetime

    @classmethod
    def from_domain(
        cls, value: BehaviorMemoryProposal
    ) -> "BehaviorMemoryProposalResponse":
        return cls.model_validate(value)


class RecoveryTraceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    draft_id: UUID
    request_id: UUID
    behavior_summary_id: UUID
    behavior_summary_fingerprint: str
    context_snapshot_reference_id: UUID
    change_impact_snapshot_id: UUID
    candidate_set_id: UUID
    candidate_set_fingerprint: str
    scope_status: RecoveryScopeStatus
    provider_name: str
    provider_version: str
    attempt_no: int
    outcome: RecoveryDraftOutcome
    fallback_used: bool
    validation_error_code: str | None
    latency_ms: float
    created_at: datetime

    @classmethod
    def from_domain(cls, value: RecoveryTrace) -> "RecoveryTraceResponse":
        return cls.model_validate(value)


class RecoveryMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    behavior_summaries_built: int
    behavior_checkins_deduplicated: int
    behavior_conflicts_detected: int
    behavior_patterns_created: int
    behavior_memory_proposals_created: int
    recovery_draft_requests: int
    recovery_scope_blocks: int
    recovery_scope_reviews: int
    recovery_primary_successes: int
    recovery_backup_successes: int
    recovery_deterministic_fallbacks: int
    recovery_complete_drafts: int
    recovery_partial_drafts: int
    recovery_no_change_drafts: int
    recovery_action_required_drafts: int
    recovery_idempotent_reuses: int
    recovery_idempotency_conflicts: int
    recovery_drafts_accepted: int
    recovery_drafts_rejected: int


class ApplyRecoveryDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=128)
    expected_draft_version: int = Field(ge=1)
    root_plan_id: UUID
    source_revision: int = Field(ge=1)
    expected_plan_version: int = Field(ge=1)
    selected_action_candidate_ids: tuple[UUID, ...] = Field(min_length=1)

    def to_domain(self) -> ApplyRecoveryDraftCommand:
        return ApplyRecoveryDraftCommand(**self.model_dump())


class RecoveryActionApplyPreviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    candidate_id: UUID
    action_type: RecoveryActionType
    target_session_id: UUID | None
    requires_session_design_draft: bool
    requires_schedule_draft: bool
    requires_plan_revision: bool
    requires_calendar_reconciliation: bool


class RecoveryApplyValidationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    passed: bool
    codes: tuple[str, ...]


class RecoveryApplyPreviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    draft_id: UUID
    draft_version: int
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    selected_actions: tuple[RecoveryActionApplyPreviewResponse, ...]
    sessions_to_preserve: tuple[UUID, ...]
    sessions_to_remove: tuple[UUID, ...]
    sessions_requiring_redesign: tuple[UUID, ...]
    sessions_requiring_reschedule: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    session_design_draft_ids: tuple[UUID, ...]
    schedule_draft_ids: tuple[UUID, ...]
    requires_session_design_drafts: bool
    requires_schedule_draft: bool
    requires_new_plan_revision: bool
    requires_calendar_reconciliation: bool
    frequency_before: int
    frequency_after: int
    validation: RecoveryApplyValidationResponse
    application_fingerprint: str

    @classmethod
    def from_domain(cls, value: RecoveryApplyPreview) -> "RecoveryApplyPreviewResponse":
        return cls.model_validate(value)


class RecoveryApplicationResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_request_id: str
    application_fingerprint: str
    recovery_draft_id: UUID
    root_plan_id: UUID
    source_revision: int
    created_revision: int | None
    applied_action_candidate_ids: tuple[UUID, ...]
    session_design_draft_ids: tuple[UUID, ...]
    schedule_draft_ids: tuple[UUID, ...]
    affected_session_ids: tuple[UUID, ...]
    removed_session_ids: tuple[UUID, ...]
    preserved_session_ids: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    outcome: str
    created_at: datetime

    @classmethod
    def from_domain(
        cls, value: RecoveryApplicationResult
    ) -> "RecoveryApplicationResultResponse":
        return cls.model_validate(value)


class RecoverySubdraftsResponse(BaseModel):
    session_design_draft_ids: tuple[UUID, ...]
    schedule_draft_ids: tuple[UUID, ...]
    created: bool


class RecoveryApplyResponse(BaseModel):
    result: RecoveryApplicationResultResponse
    draft: RecoveryDraftResponse
    plan: WeeklyPlanResponse | None
    created: bool


class RecoveryMemoryProposalSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_proposal_ids: tuple[UUID, ...] = Field(min_length=1)


class ImportRecoveryMemoryProposalsRequest(RecoveryMemoryProposalSelectionRequest):
    client_request_id: str = Field(min_length=1, max_length=128)
    expected_draft_version: int = Field(ge=1)

    def to_domain(self) -> ImportRecoveryMemoryProposalsCommand:
        return ImportRecoveryMemoryProposalsCommand(**self.model_dump())


class RecoveryMemoryProposalPreviewItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    proposal_id: UUID
    memory_type: str
    proposed_key: str
    proposed_value: str
    evidence_count: int
    duplicate: bool
    conflict: bool
    importable: bool
    reason_codes: tuple[str, ...]


class RecoveryMemoryProposalPreviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    draft_id: UUID
    draft_version: int
    items: tuple[RecoveryMemoryProposalPreviewItemResponse, ...]
    fingerprint: str

    @classmethod
    def from_domain(
        cls, value: RecoveryMemoryProposalPreview
    ) -> "RecoveryMemoryProposalPreviewResponse":
        return cls.model_validate(value)


class RecoveryMemoryProposalImportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    draft_id: UUID
    client_request_id: str
    fingerprint: str
    proposal_ids: tuple[UUID, ...]
    memory_candidate_ids: tuple[UUID, ...]
    created_at: datetime

    @classmethod
    def from_domain(
        cls, value: RecoveryMemoryProposalImportResult
    ) -> "RecoveryMemoryProposalImportResponse":
        return cls.model_validate(value)
