"""Immutable Recovery application commands, previews, metadata, and results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.common import (
    DomainValidationError,
    require_non_blank,
    require_utc_datetime,
    require_version,
)
from app.domain.recovery.enums import RecoveryActionType
from app.domain.recovery_application.enums import RecoveryApplicationOutcome

ACTION_RESOLUTION_POLICY_VERSION = "recovery-action-resolution-v1"
PLAN_APPLICATION_POLICY_VERSION = "recovery-plan-application-v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyRecoveryDraftCommand:
    client_request_id: str
    expected_draft_version: int
    root_plan_id: UUID
    source_revision: int
    expected_plan_version: int
    selected_action_candidate_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        require_version(self.expected_draft_version)
        require_version(self.source_revision)
        require_version(self.expected_plan_version)
        if not self.selected_action_candidate_ids:
            raise DomainValidationError("selected Recovery action IDs cannot be empty.")
        if len(self.selected_action_candidate_ids) != len(
            set(self.selected_action_candidate_ids)
        ):
            raise DomainValidationError("selected Recovery action IDs must be unique.")
        object.__setattr__(self, "client_request_id", self.client_request_id.strip())
        object.__setattr__(
            self,
            "selected_action_candidate_ids",
            tuple(sorted(self.selected_action_candidate_ids, key=str)),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryActionPreview:
    candidate_id: UUID
    action_type: RecoveryActionType
    target_session_id: UUID | None
    requires_session_design_draft: bool
    requires_schedule_draft: bool
    requires_plan_revision: bool
    requires_calendar_reconciliation: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryApplyValidation:
    passed: bool
    codes: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryApplyPreview:
    draft_id: UUID
    draft_version: int
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    selected_actions: tuple[RecoveryActionPreview, ...]
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
    validation: RecoveryApplyValidation
    application_fingerprint: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryPlanApplicationMetadata:
    source_recovery_draft_id: UUID
    source_recovery_draft_version: int
    source_candidate_set_id: UUID
    source_behavior_summary_id: UUID
    source_change_impact_snapshot_id: UUID
    source_revision: int
    created_revision: int
    applied_action_candidate_ids: tuple[UUID, ...]
    session_design_draft_ids: tuple[UUID, ...]
    schedule_draft_ids: tuple[UUID, ...]
    changed_session_ids: tuple[UUID, ...]
    removed_session_ids: tuple[UUID, ...]
    preserved_session_ids: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    application_fingerprint: str
    policy_version: str = PLAN_APPLICATION_POLICY_VERSION

    def __post_init__(self) -> None:
        require_non_blank(self.application_fingerprint, "application_fingerprint")
        require_non_blank(self.policy_version, "policy_version")
        if self.created_revision != self.source_revision + 1:
            raise DomainValidationError("Recovery revision numbering is invalid.")
        for name in (
            "applied_action_candidate_ids",
            "session_design_draft_ids",
            "schedule_draft_ids",
            "changed_session_ids",
            "removed_session_ids",
            "preserved_session_ids",
            "immutable_session_ids",
        ):
            values = getattr(self, name)
            if values != tuple(sorted(set(values), key=str)):
                raise DomainValidationError(f"{name} must be unique and sorted.")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryApplicationResult:
    id: UUID
    user_id: UUID
    client_request_id: str
    application_fingerprint: str
    request_fingerprint: str
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
    outcome: RecoveryApplicationOutcome
    created_at: datetime

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        require_non_blank(self.application_fingerprint, "application_fingerprint")
        require_non_blank(self.request_fingerprint, "request_fingerprint")
        require_utc_datetime(self.created_at, "created_at")
        if self.outcome is RecoveryApplicationOutcome.PLAN_REVISION_CREATED:
            if self.created_revision != self.source_revision + 1:
                raise DomainValidationError("Recovery result revision is invalid.")
        elif self.created_revision is not None:
            raise DomainValidationError("no-change result cannot reference a revision.")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryApplicationCommit:
    result: RecoveryApplicationResult
    plan_revision_id: UUID | None
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryMemoryProposalPreviewItem:
    proposal_id: UUID
    memory_type: str
    proposed_key: str
    proposed_value: str
    evidence_count: int
    duplicate: bool
    conflict: bool
    importable: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryMemoryProposalPreview:
    draft_id: UUID
    draft_version: int
    items: tuple[RecoveryMemoryProposalPreviewItem, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportRecoveryMemoryProposalsCommand:
    client_request_id: str
    expected_draft_version: int
    selected_proposal_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        require_version(self.expected_draft_version)
        if not self.selected_proposal_ids:
            raise DomainValidationError("at least one behavior proposal is required.")
        if len(self.selected_proposal_ids) != len(set(self.selected_proposal_ids)):
            raise DomainValidationError("behavior proposal IDs must be unique.")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryMemoryProposalImportResult:
    id: UUID
    user_id: UUID
    draft_id: UUID
    client_request_id: str
    fingerprint: str
    proposal_ids: tuple[UUID, ...]
    memory_candidate_ids: tuple[UUID, ...]
    created_at: datetime
