"""Immutable Recovery snapshots, candidates, Drafts, and safe traces."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.behavior.models import BehaviorSummaryWindow
from app.domain.common import (
    DomainConflictError,
    DomainValidationError,
    InvalidDomainStateTransition,
    require_non_blank,
    require_utc_datetime,
    require_version,
)
from app.domain.recovery.enums import (
    RecoveryActionType,
    RecoveryDraftOutcome,
    RecoveryDraftSource,
    RecoveryDraftStatus,
    RecoveryRedesignGoal,
    RecoveryRequestType,
    RecoveryScopeStatus,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateRecoveryDraftCommand:
    client_request_id: str
    root_plan_id: UUID
    source_revision: int
    expected_plan_version: int
    request_type: RecoveryRequestType
    target_session_ids: tuple[UUID, ...] | None
    user_request: str
    behavior_window: BehaviorSummaryWindow | None = None

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        require_non_blank(self.user_request, "user_request")
        if len(self.client_request_id.strip()) > 128:
            raise DomainValidationError("client_request_id is too long.")
        if len(self.user_request.strip()) > 1000:
            raise DomainValidationError("user_request must not exceed 1000 characters.")
        if self.source_revision < 1 or self.expected_plan_version < 1:
            raise DomainValidationError(
                "source revision and plan version must be positive."
            )
        if self.target_session_ids is not None:
            if not self.target_session_ids:
                raise DomainValidationError(
                    "target_session_ids cannot be empty when provided."
                )
            if len(set(self.target_session_ids)) != len(self.target_session_ids):
                raise DomainValidationError("target_session_ids must be unique.")
            object.__setattr__(
                self,
                "target_session_ids",
                tuple(sorted(self.target_session_ids, key=str)),
            )
        object.__setattr__(self, "client_request_id", self.client_request_id.strip())
        object.__setattr__(self, "user_request", self.user_request.strip())


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryChangeImpactSnapshot:
    id: UUID
    user_id: UUID
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

    def __post_init__(self) -> None:
        require_non_blank(self.fingerprint, "fingerprint")
        require_utc_datetime(self.created_at, "created_at")
        for name in (
            "mutable_session_ids",
            "immutable_session_ids",
            "preserved_session_ids",
            "calendar_bound_session_ids",
            "completed_checkin_ids",
        ):
            values = getattr(self, name)
            if values != tuple(sorted(set(values), key=str)):
                raise DomainValidationError(f"{name} must be unique and stably sorted.")
        if set(self.mutable_session_ids) & set(self.immutable_session_ids):
            raise DomainValidationError(
                "mutable and immutable Sessions cannot overlap."
            )
        if not (
            2
            <= self.minimum_allowed_frequency
            <= self.weekly_frequency_before
            <= self.maximum_allowed_frequency
            <= 5
        ):
            raise DomainValidationError("weekly frequency bounds are invalid.")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryActionCandidate:
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

    def __post_init__(self) -> None:
        session_actions = {
            RecoveryActionType.REQUEST_SESSION_RESCHEDULE,
            RecoveryActionType.REQUEST_SESSION_REDESIGN,
            RecoveryActionType.REMOVE_FUTURE_SESSION,
        }
        if (self.action_type in session_actions) != (
            self.target_session_id is not None
        ):
            raise DomainValidationError(
                "session Recovery actions require one target Session."
            )
        if self.action_type is RecoveryActionType.REQUEST_SESSION_REDESIGN:
            if self.redesign_goal is None:
                raise DomainValidationError(
                    "redesign action requires a controlled goal."
                )
        elif self.redesign_goal is not None:
            raise DomainValidationError("only redesign actions may set redesign_goal.")
        if self.action_type is RecoveryActionType.NEXT_WEEK_FREQUENCY_REVIEW:
            if self.target_week_start is None:
                raise DomainValidationError(
                    "next-week review requires target_week_start."
                )
        elif self.target_week_start is not None:
            raise DomainValidationError("only next-week review may target a week.")
        if self.deterministic_rank < 0:
            raise DomainValidationError("deterministic_rank cannot be negative.")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryActionCandidateSet:
    id: UUID
    user_id: UUID
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    behavior_summary_id: UUID
    behavior_summary_fingerprint: str
    context_snapshot_reference_id: UUID
    context_fingerprint: str
    change_impact_snapshot_id: UUID
    candidates: tuple[RecoveryActionCandidate, ...]
    fingerprint: str
    policy_version: str
    prompt_version: str
    created_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.behavior_summary_fingerprint, "behavior_summary_fingerprint"),
            (self.context_fingerprint, "context_fingerprint"),
            (self.fingerprint, "fingerprint"),
            (self.policy_version, "policy_version"),
            (self.prompt_version, "prompt_version"),
        ):
            require_non_blank(value, name)
        require_utc_datetime(self.created_at, "created_at")
        ids = tuple(item.id for item in self.candidates)
        if len(ids) != len(set(ids)):
            raise DomainValidationError("Recovery Candidate IDs must be unique.")
        if self.candidates != tuple(
            sorted(
                self.candidates,
                key=lambda item: (item.deterministic_rank, str(item.id)),
            )
        ):
            raise DomainValidationError("Recovery Candidates must use stable ordering.")

    @property
    def candidate_ids(self) -> tuple[UUID, ...]:
        return tuple(item.id for item in self.candidates)

    @property
    def candidate_map(self) -> dict[UUID, RecoveryActionCandidate]:
        return {item.id: item for item in self.candidates}


class RecoveryAgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_action_candidate_ids: tuple[UUID, ...]
    unresolved_session_ids: tuple[UUID, ...]
    explanation_summary: str = Field(min_length=1, max_length=500)

    @field_validator("selected_action_candidate_ids", "unresolved_session_ids")
    @classmethod
    def require_unique_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(value) != len(set(value)):
            raise ValueError("IDs must not be duplicated")
        return value

    @field_validator("explanation_summary")
    @classmethod
    def safe_explanation(cls, value: str) -> str:
        normalized = value.strip()
        lowered = normalized.casefold()
        prohibited = (
            "```",
            "tool_call",
            "diagnos",
            "medication",
            "处方",
            "诊断",
            "胸痛",
            "晕厥",
        )
        if any(item in lowered for item in prohibited):
            raise ValueError("explanation contains prohibited content")
        return normalized


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoverySpacingValidation:
    passed: bool
    violation_codes: tuple[str, ...]
    affected_session_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryDraft:
    id: UUID
    request_id: UUID
    client_request_id: str
    user_id: UUID
    request_payload_fingerprint: str
    request_fingerprint: str
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
    reviewed_at: datetime | None = None
    application_result_id: UUID | None = None
    applied_root_plan_id: UUID | None = None
    applied_source_revision: int | None = None
    applied_created_revision: int | None = None
    applied_session_ids: tuple[UUID, ...] = ()
    created_session_design_draft_ids: tuple[UUID, ...] = ()
    created_schedule_draft_ids: tuple[UUID, ...] = ()
    applied_at: datetime | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.client_request_id, "client_request_id"),
            (self.request_payload_fingerprint, "request_payload_fingerprint"),
            (self.request_fingerprint, "request_fingerprint"),
            (self.explanation_summary, "explanation_summary"),
        ):
            require_non_blank(value, name)
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.expires_at, "expires_at")
        require_version(self.version)
        if self.expires_at <= self.created_at:
            raise DomainValidationError("Recovery Draft expiry must be in the future.")
        if self.status is RecoveryDraftStatus.PENDING_REVIEW:
            if self.reviewed_at is not None:
                raise DomainValidationError(
                    "pending Recovery Draft cannot be reviewed."
                )
        elif self.reviewed_at is None:
            raise DomainValidationError("terminal Recovery Draft requires reviewed_at.")
        audit_values = (
            self.application_result_id,
            self.applied_root_plan_id,
            self.applied_source_revision,
            self.applied_at,
        )
        if self.status is RecoveryDraftStatus.APPLIED:
            if any(value is None for value in audit_values):
                raise DomainValidationError(
                    "APPLIED Recovery Draft requires application audit references."
                )
            assert self.applied_at is not None
            require_utc_datetime(self.applied_at, "applied_at")
            if self.applied_created_revision is not None:
                if self.applied_created_revision != self.source_revision + 1:
                    raise DomainValidationError(
                        "created Recovery revision must follow the source revision."
                    )
        elif any(
            value is not None
            for value in (*audit_values, self.applied_created_revision)
        ):
            raise DomainValidationError(
                "only APPLIED Recovery Draft may contain application audit fields."
            )
        for name in (
            "applied_session_ids",
            "created_session_design_draft_ids",
            "created_schedule_draft_ids",
        ):
            values = getattr(self, name)
            if values != tuple(sorted(set(values), key=str)):
                raise DomainValidationError(f"{name} must be unique and sorted.")

    def accept(self, *, expected_version: int, at: datetime) -> RecoveryDraft:
        self._require_pending(expected_version, at)
        if self.outcome not in {
            RecoveryDraftOutcome.COMPLETE,
            RecoveryDraftOutcome.NO_CHANGE,
        }:
            raise InvalidDomainStateTransition(
                "this Recovery Draft cannot be accepted."
            )
        return replace(
            self,
            status=RecoveryDraftStatus.ACCEPTED,
            reviewed_at=at,
            version=self.version + 1,
        )

    def reject(self, *, expected_version: int, at: datetime) -> RecoveryDraft:
        self._require_pending(expected_version, at)
        return replace(
            self,
            status=RecoveryDraftStatus.REJECTED,
            reviewed_at=at,
            version=self.version + 1,
        )

    def expire(self, at: datetime) -> RecoveryDraft:
        require_utc_datetime(at, "expired_at")
        if self.status is not RecoveryDraftStatus.PENDING_REVIEW:
            raise InvalidDomainStateTransition(
                "only a pending Recovery Draft can expire."
            )
        return replace(
            self,
            status=RecoveryDraftStatus.EXPIRED,
            reviewed_at=at,
            version=self.version + 1,
        )

    def mark_applied(
        self,
        *,
        application_result_id: UUID,
        root_plan_id: UUID,
        source_revision: int,
        created_revision: int | None,
        affected_session_ids: tuple[UUID, ...],
        session_design_draft_ids: tuple[UUID, ...],
        schedule_draft_ids: tuple[UUID, ...],
        at: datetime,
    ) -> RecoveryDraft:
        """Record the final application outcome without mutating draft content."""

        require_utc_datetime(at, "applied_at")
        if self.status is not RecoveryDraftStatus.ACCEPTED:
            raise InvalidDomainStateTransition(
                "only an ACCEPTED Recovery Draft can be applied."
            )
        if source_revision != self.source_revision or root_plan_id != self.root_plan_id:
            raise DomainValidationError(
                "application references must match the frozen Recovery Draft."
            )
        if created_revision is not None and created_revision != source_revision + 1:
            raise DomainValidationError(
                "created revision must immediately follow the source revision."
            )
        return replace(
            self,
            status=RecoveryDraftStatus.APPLIED,
            application_result_id=application_result_id,
            applied_root_plan_id=root_plan_id,
            applied_source_revision=source_revision,
            applied_created_revision=created_revision,
            applied_session_ids=tuple(sorted(set(affected_session_ids), key=str)),
            created_session_design_draft_ids=tuple(
                sorted(set(session_design_draft_ids), key=str)
            ),
            created_schedule_draft_ids=tuple(sorted(set(schedule_draft_ids), key=str)),
            applied_at=at,
            version=self.version + 1,
        )

    def _require_pending(self, expected_version: int, at: datetime) -> None:
        require_utc_datetime(at, "reviewed_at")
        if expected_version != self.version:
            raise DomainConflictError("Recovery Draft version conflict.")
        if self.status is not RecoveryDraftStatus.PENDING_REVIEW:
            raise InvalidDomainStateTransition("Recovery Draft is already terminal.")
        if at >= self.expires_at:
            raise InvalidDomainStateTransition("Recovery Draft has expired.")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryTrace:
    id: UUID
    user_id: UUID
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

    def __post_init__(self) -> None:
        require_utc_datetime(self.created_at, "created_at")
        if self.attempt_no < 0 or self.latency_ms < 0:
            raise DomainValidationError("trace attempt and latency cannot be negative.")
