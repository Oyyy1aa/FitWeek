"""Serializable Context output and privacy-preserving audit values."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

from app.domain.common import (
    DomainValidationError,
    require_non_blank,
    require_utc_datetime,
)
from app.domain.context.enums import AgentType, ContextDegradedMode, ContextSectionName


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextItem:
    key: str
    value: str
    source: str
    source_reference: str | None = None

    def __post_init__(self) -> None:
        require_non_blank(self.key, "key")
        require_non_blank(self.value, "value")
        require_non_blank(self.source, "source")
        if self.source_reference is not None:
            require_non_blank(self.source_reference, "source_reference")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextSection:
    name: ContextSectionName
    source: str
    generated_at: datetime
    version: str
    items: tuple[ContextItem, ...]

    def __post_init__(self) -> None:
        require_utc_datetime(self.generated_at, "generated_at")
        require_non_blank(self.source, "source")
        require_non_blank(self.version, "version")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextConflict:
    higher_priority_source: str
    lower_priority_source: str
    key: str
    resolution: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextBuildAudit:
    id: UUID
    user_id: UUID
    agent_type: AgentType
    context_contract_version: str
    request_fingerprint: str
    included_memory_ids: tuple[UUID, ...]
    excluded_memory_ids: tuple[UUID, ...]
    exclusion_reasons: Mapping[UUID, str]
    conflicts: tuple[ContextConflict, ...]
    budget_before: int
    budget_after: int
    degraded_mode: ContextDegradedMode
    created_at: datetime
    run_id: UUID | None = None
    step_id: UUID | None = None
    profile_draft_id: UUID | None = None
    plan_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.user_id, UUID):
            raise DomainValidationError("audit id and user_id must be UUID values.")
        require_utc_datetime(self.created_at, "created_at")
        if self.budget_before < 0 or self.budget_after < 0:
            raise DomainValidationError("budget counts cannot be negative.")
        object.__setattr__(self, "exclusion_reasons", dict(self.exclusion_reasons))


@dataclass(frozen=True, slots=True, kw_only=True, order=True)
class EntityVersionReference:
    id: UUID
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise DomainValidationError("entity reference id must be a UUID.")
        if self.version < 1:
            raise DomainValidationError("entity reference version must be positive.")


@dataclass(frozen=True, slots=True, kw_only=True)
class BuiltContext:
    id: UUID
    user_id: UUID
    agent_type: AgentType
    contract_version: str
    sections: tuple[ContextSection, ...]
    conflicts: tuple[ContextConflict, ...]
    degraded_mode: ContextDegradedMode
    character_count: int
    audit_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextSnapshotReference:
    id: UUID
    user_id: UUID
    agent_type: AgentType
    contract_version: str
    policy_version: str
    context_fingerprint: str
    context_audit_id: UUID
    profile_id: UUID | None
    profile_version: int | None
    constraint_versions: tuple[EntityVersionReference, ...]
    memory_versions: tuple[EntityVersionReference, ...]
    degraded_mode: ContextDegradedMode
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "user_id", "context_audit_id"):
            if not isinstance(getattr(self, name), UUID):
                raise DomainValidationError(f"{name} must be a UUID.")
        if (self.profile_id is None) != (self.profile_version is None):
            raise DomainValidationError(
                "profile id and version must be present or absent together."
            )
        if self.profile_version is not None and self.profile_version < 1:
            raise DomainValidationError("profile_version must be positive.")
        require_non_blank(self.contract_version, "contract_version")
        require_non_blank(self.policy_version, "policy_version")
        require_non_blank(self.context_fingerprint, "context_fingerprint")
        require_utc_datetime(self.created_at, "created_at")
        if tuple(sorted(self.constraint_versions)) != self.constraint_versions:
            raise DomainValidationError(
                "constraint versions must use stable ID ordering."
            )
        if tuple(sorted(self.memory_versions)) != self.memory_versions:
            raise DomainValidationError("memory versions must use stable ID ordering.")


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenContextSnapshot:
    """Private process-local payload; only its reference crosses service boundaries."""

    reference: ContextSnapshotReference
    context: BuiltContext


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextBuildCommand:
    agent_type: AgentType
    current_task: Mapping[str, str]
    recent_behavior_summary: tuple[str, ...] = ()
    catalog_reference: str | None = None
    max_characters: int | None = None
    run_id: UUID | None = None
    step_id: UUID | None = None
    profile_draft_id: UUID | None = None
    plan_id: UUID | None = None

    def __post_init__(self) -> None:
        if not self.current_task:
            raise DomainValidationError("current_task must contain structured values.")
        normalized = {
            key.strip(): value.strip() for key, value in self.current_task.items()
        }
        if any(not key or not value for key, value in normalized.items()):
            raise DomainValidationError(
                "current_task keys and values must not be blank."
            )
        if len(normalized) > 20:
            raise DomainValidationError("current_task may contain at most 20 values.")
        if any(len(value) > 240 for value in normalized.values()):
            raise DomainValidationError(
                "current_task values must not exceed 240 characters."
            )
        if len(self.recent_behavior_summary) > 20:
            raise DomainValidationError("recent behavior may contain at most 20 items.")
        if self.max_characters is not None and self.max_characters < 128:
            raise DomainValidationError("max_characters must be at least 128.")
        object.__setattr__(self, "current_task", MappingProxyType(normalized))
