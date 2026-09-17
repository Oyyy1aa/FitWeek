"""Strict Profile Agent contract values; none are persisted profile entities."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.common import LocationType, require_utc_datetime
from app.domain.context.enums import ContextDegradedMode
from app.domain.context.models import BuiltContext
from app.domain.model_gateway.enums import FallbackType
from app.domain.profiles.models import ConstraintType, FitnessGoal


class ScopeStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ProfileDraftStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ConstraintProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    constraint_type: ConstraintType
    value: str = Field(min_length=1, max_length=240)
    is_hard: bool
    priority: int = Field(default=50, ge=0, le=100)
    valid_until: datetime | None = None

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("constraint value must not be blank")
        return normalized

    @field_validator("valid_until")
    @classmethod
    def require_aware_valid_until(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            require_utc_datetime(value, "valid_until")
        return value


class PreferenceProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preference_type: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=240)


class MemoryCandidateProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str = Field(pattern="^PREFERENCE$")
    value: str = Field(min_length=1, max_length=240)
    rationale: str = Field(min_length=1, max_length=300)


class ProfileAgentOutput(BaseModel):
    """Untrusted model output after strict schema parsing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    weekly_frequency: int | None = Field(default=None, ge=2, le=5)
    max_session_minutes: int | None = Field(default=None, ge=15, le=60)
    goals: tuple[FitnessGoal, ...] = ()
    hard_constraints: tuple[ConstraintProposal, ...] = ()
    soft_preferences: tuple[PreferenceProposal, ...] = ()
    temporary_constraints: tuple[ConstraintProposal, ...] = ()
    equipment: tuple[str, ...] = ()
    locations: tuple[LocationType, ...] = ()
    scope_status: ScopeStatus
    missing_fields: tuple[str, ...] = ()
    memory_candidates: tuple[MemoryCandidateProposal, ...] = ()
    explanation_summary: str = Field(max_length=800)

    @field_validator("equipment", "missing_fields")
    @classmethod
    def normalize_string_tuple(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("list values must not be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("list values must be unique")
        return normalized


@dataclass(frozen=True, slots=True, kw_only=True)
class UserProfileSnapshot:
    experience_level: str
    weekly_frequency: int
    max_session_minutes: int
    primary_goal: str
    scope_confirmed: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileAgentInput:
    user_message: str
    current_week: date
    existing_profile: UserProfileSnapshot | None
    supported_goals: tuple[str, ...]
    supported_constraint_types: tuple[str, ...]
    supported_equipment: tuple[str, ...]
    supported_locations: tuple[str, ...]
    context: BuiltContext | None = None
    context_snapshot_reference_id: UUID | None = None
    context_fingerprint: str | None = None
    context_contract_version: str | None = None
    context_degraded_mode: ContextDegradedMode = ContextDegradedMode.NONE
    context_included_memory_count: int = 0

    def __post_init__(self) -> None:
        message = self.user_message.strip()
        if not message or len(message) > 4000:
            raise ValueError("user_message must contain between 1 and 4000 characters")
        if self.current_week.weekday() != 0:
            raise ValueError("current_week must be a Monday")
        for values in (
            self.supported_goals,
            self.supported_constraint_types,
            self.supported_equipment,
            self.supported_locations,
        ):
            if not values or any(not value for value in values):
                raise ValueError("supported value lists must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileAgentResult:
    request_id: UUID
    output: ProfileAgentOutput
    input_fingerprint: str
    prompt_version: str
    provider_summary: str
    fallback_used: bool
    fallback_type: FallbackType | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileAgentDraft:
    id: UUID
    request_id: UUID
    client_request_id: str
    user_id: UUID
    request_payload_fingerprint: str
    input_fingerprint: str
    output: ProfileAgentOutput
    prompt_version: str
    provider_summary: str
    fallback_used: bool
    fallback_type: FallbackType | None
    created_at: datetime
    expires_at: datetime
    status: ProfileDraftStatus = ProfileDraftStatus.PENDING_REVIEW
    version: int = 1
    applied_at: datetime | None = None
    rejected_at: datetime | None = None
    applied_profile_id: UUID | None = None
    apply_request_id: str | None = None
    context_snapshot_reference_id: UUID | None = None
    context_fingerprint: str | None = None
    context_contract_version: str | None = None
    context_policy_version: str | None = None
    context_degraded_mode: ContextDegradedMode = ContextDegradedMode.NONE
    context_included_memory_count: int = 0

    def __post_init__(self) -> None:
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.version < 1:
            raise ValueError("draft version must be positive")
        if self.status is ProfileDraftStatus.PENDING_REVIEW:
            if any(
                value is not None
                for value in (
                    self.applied_at,
                    self.rejected_at,
                    self.applied_profile_id,
                    self.apply_request_id,
                )
            ):
                raise ValueError("pending draft cannot contain terminal metadata")
        elif self.status is ProfileDraftStatus.APPLIED:
            if (
                self.applied_at is None
                or self.applied_profile_id is None
                or not self.apply_request_id
                or self.rejected_at is not None
            ):
                raise ValueError("applied draft requires apply metadata")
            require_utc_datetime(self.applied_at, "applied_at")
        elif self.status is ProfileDraftStatus.REJECTED:
            if self.rejected_at is None or any(
                value is not None
                for value in (
                    self.applied_at,
                    self.applied_profile_id,
                    self.apply_request_id,
                )
            ):
                raise ValueError("rejected draft requires rejected_at only")
            require_utc_datetime(self.rejected_at, "rejected_at")
        elif self.status is ProfileDraftStatus.EXPIRED and any(
            value is not None
            for value in (
                self.applied_at,
                self.rejected_at,
                self.applied_profile_id,
                self.apply_request_id,
            )
        ):
            raise ValueError("expired draft cannot contain apply metadata")

    def mark_applied(
        self,
        *,
        applied_at: datetime,
        profile_id: UUID,
        apply_request_id: str,
    ) -> ProfileAgentDraft:
        if self.status is not ProfileDraftStatus.PENDING_REVIEW:
            raise ValueError("only a pending draft can be applied")
        return replace(
            self,
            status=ProfileDraftStatus.APPLIED,
            applied_at=applied_at,
            applied_profile_id=profile_id,
            apply_request_id=apply_request_id,
            version=self.version + 1,
        )

    def mark_rejected(self, *, rejected_at: datetime) -> ProfileAgentDraft:
        if self.status is not ProfileDraftStatus.PENDING_REVIEW:
            raise ValueError("only a pending draft can be rejected")
        return replace(
            self,
            status=ProfileDraftStatus.REJECTED,
            rejected_at=rejected_at,
            version=self.version + 1,
        )

    def mark_expired(self) -> ProfileAgentDraft:
        if self.status is not ProfileDraftStatus.PENDING_REVIEW:
            return self
        return replace(
            self,
            status=ProfileDraftStatus.EXPIRED,
            version=self.version + 1,
        )
