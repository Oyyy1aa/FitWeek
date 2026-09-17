"""Deterministic Profile Draft review, preview, and apply values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from app.domain.common import LocationType, require_utc_datetime
from app.domain.context.enums import ContextDegradedMode
from app.domain.profile_agent.models import (
    MemoryCandidateProposal,
    PreferenceProposal,
)
from app.domain.profiles.models import (
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
    FitnessProfile,
    UserConstraint,
)

APPLY_POLICY_VERSION = "profile-draft-apply-v1"


class ApplyValidationCode(StrEnum):
    INVALID_DRAFT_SELECTION = "INVALID_DRAFT_SELECTION"
    PRIMARY_GOAL_SELECTION_REQUIRED = "PRIMARY_GOAL_SELECTION_REQUIRED"
    EXPERIENCE_LEVEL_REQUIRED = "EXPERIENCE_LEVEL_REQUIRED"
    SCOPE_CONFIRMATION_REQUIRED = "SCOPE_CONFIRMATION_REQUIRED"
    TEMPORARY_CONSTRAINT_EXPIRATION_REQUIRED = (
        "TEMPORARY_CONSTRAINT_EXPIRATION_REQUIRED"
    )
    CONSTRAINT_CONFLICT = "CONSTRAINT_CONFLICT"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileDraftApplyDecision:
    client_request_id: str
    expected_draft_version: int
    expected_profile_version: int | None
    accept_weekly_frequency: bool
    accept_max_session_minutes: bool
    selected_primary_goal: FitnessGoal | None
    accepted_equipment: tuple[str, ...]
    accepted_locations: tuple[LocationType, ...]
    accepted_hard_constraint_indexes: tuple[int, ...]
    accepted_temporary_constraint_indexes: tuple[int, ...]
    temporary_constraint_expirations: Mapping[int, datetime]
    confirmed_experience_level: ExperienceLevel | None
    confirm_scope: bool

    def __post_init__(self) -> None:
        request_id = self.client_request_id.strip()
        if not request_id:
            raise ValueError("client_request_id must not be blank")
        if self.expected_draft_version < 1:
            raise ValueError("expected_draft_version must be positive")
        if (
            self.expected_profile_version is not None
            and self.expected_profile_version < 1
        ):
            raise ValueError("expected_profile_version must be positive")
        for values, name in (
            (self.accepted_equipment, "accepted_equipment"),
            (self.accepted_locations, "accepted_locations"),
            (self.accepted_hard_constraint_indexes, "hard indexes"),
            (self.accepted_temporary_constraint_indexes, "temporary indexes"),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must be unique")
        if any(not item.strip() for item in self.accepted_equipment):
            raise ValueError("accepted equipment must not be blank")
        expirations = dict(self.temporary_constraint_expirations)
        for index, expiration in expirations.items():
            if index < 0:
                raise ValueError("temporary constraint indexes must not be negative")
            require_utc_datetime(expiration, "temporary constraint expiration")
        object.__setattr__(self, "client_request_id", request_id)
        object.__setattr__(
            self,
            "temporary_constraint_expirations",
            MappingProxyType(expirations),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileFieldChange:
    field_name: str
    old_value: str | int | bool | None
    new_value: str | int | bool | None
    source: str = "PROFILE_AGENT_DRAFT_USER_SELECTION"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConstraintPreview:
    constraint_id: UUID | None
    constraint_type: ConstraintType
    constraint_value: str
    priority: int
    is_hard: bool
    valid_until: datetime | None
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ConstraintConflict:
    constraint_type: ConstraintType
    existing_value: str
    proposed_value: str
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class FitnessProfilePreview:
    profile_id: UUID
    experience_level: ExperienceLevel
    weekly_frequency: int
    max_session_minutes: int
    primary_goal: FitnessGoal
    scope_confirmed: bool
    resulting_version: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyValidationIssue:
    code: ApplyValidationCode
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyValidationResult:
    passed: bool
    issues: tuple[ApplyValidationIssue, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileDraftApplyPreview:
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
    policy_version: str = APPLY_POLICY_VERSION


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileDraftApplyResult:
    id: UUID
    user_id: UUID
    draft_id: UUID
    client_request_id: str
    apply_fingerprint: str
    profile_id: UUID
    previous_profile_version: int | None
    resulting_profile_version: int
    added_constraint_ids: tuple[UUID, ...]
    unchanged_constraint_ids: tuple[UUID, ...]
    ignored_soft_preference_count: int
    ignored_memory_candidate_count: int
    applied_at: datetime

    def __post_init__(self) -> None:
        if not self.client_request_id.strip():
            raise ValueError("client_request_id must not be blank")
        if len(self.apply_fingerprint) != 64:
            raise ValueError("apply_fingerprint must be SHA-256")
        if self.resulting_profile_version < 1:
            raise ValueError("resulting_profile_version must be positive")
        require_utc_datetime(self.applied_at, "applied_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileDraftRejectResult:
    draft_id: UUID
    user_id: UUID
    client_request_id: str
    reject_fingerprint: str
    rejected_at: datetime
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileDraftMergeOutcome:
    preview: ProfileDraftApplyPreview
    profile: FitnessProfile
    constraints_to_add: tuple[UserConstraint, ...]
    expected_constraint_versions: tuple[tuple[UUID, int], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileDraftCommitResult:
    result: ProfileDraftApplyResult
    created: bool
