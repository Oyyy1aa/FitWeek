"""Strict Phase 4A Context build and Audit HTTP contracts."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.api.contracts import RequestModel
from app.domain.context.enums import AgentType, ContextDegradedMode, ContextSectionName
from app.domain.context.models import (
    BuiltContext,
    ContextBuildAudit,
    ContextConflict,
    ContextItem,
    ContextSection,
    ContextSnapshotReference,
    EntityVersionReference,
)


class BuildContextRequest(RequestModel):
    agent_type: AgentType
    current_task: dict[str, str] = Field(min_length=1, max_length=20)
    recent_behavior_summary: tuple[str, ...] = Field(default=(), max_length=20)
    catalog_reference: str | None = Field(default=None, max_length=160)
    max_characters: int | None = Field(default=None, ge=128)


class ContextItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    value: str
    source: str
    source_reference: str | None

    @classmethod
    def from_domain(cls, value: ContextItem) -> Self:
        return cls.model_validate(value)


class ContextSectionResponse(BaseModel):
    name: ContextSectionName
    source: str
    generated_at: datetime
    version: str
    items: tuple[ContextItemResponse, ...]

    @classmethod
    def from_domain(cls, value: ContextSection) -> Self:
        return cls(
            name=value.name,
            source=value.source,
            generated_at=value.generated_at,
            version=value.version,
            items=tuple(ContextItemResponse.from_domain(item) for item in value.items),
        )


class ContextConflictResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    higher_priority_source: str
    lower_priority_source: str
    key: str
    resolution: str

    @classmethod
    def from_domain(cls, value: ContextConflict) -> Self:
        return cls.model_validate(value)


class BuiltContextResponse(BaseModel):
    id: UUID
    agent_type: AgentType
    contract_version: str
    sections: tuple[ContextSectionResponse, ...]
    conflicts: tuple[ContextConflictResponse, ...]
    degraded_mode: ContextDegradedMode
    character_count: int
    audit_id: UUID
    created_at: datetime

    @classmethod
    def from_domain(cls, value: BuiltContext) -> Self:
        return cls(
            id=value.id,
            agent_type=value.agent_type,
            contract_version=value.contract_version,
            sections=tuple(
                ContextSectionResponse.from_domain(item) for item in value.sections
            ),
            conflicts=tuple(
                ContextConflictResponse.from_domain(item) for item in value.conflicts
            ),
            degraded_mode=value.degraded_mode,
            character_count=value.character_count,
            audit_id=value.audit_id,
            created_at=value.created_at,
        )


class ContextAuditResponse(BaseModel):
    id: UUID
    agent_type: AgentType
    context_contract_version: str
    request_fingerprint: str
    included_memory_ids: tuple[UUID, ...]
    excluded_memory_ids: tuple[UUID, ...]
    exclusion_reasons: dict[UUID, str]
    conflicts: tuple[ContextConflictResponse, ...]
    budget_before: int
    budget_after: int
    degraded_mode: ContextDegradedMode
    created_at: datetime
    run_id: UUID | None
    step_id: UUID | None
    profile_draft_id: UUID | None
    plan_id: UUID | None

    @classmethod
    def from_domain(cls, value: ContextBuildAudit) -> Self:
        return cls(
            id=value.id,
            agent_type=value.agent_type,
            context_contract_version=value.context_contract_version,
            request_fingerprint=value.request_fingerprint,
            included_memory_ids=value.included_memory_ids,
            excluded_memory_ids=value.excluded_memory_ids,
            exclusion_reasons=dict(value.exclusion_reasons),
            conflicts=tuple(
                ContextConflictResponse.from_domain(item) for item in value.conflicts
            ),
            budget_before=value.budget_before,
            budget_after=value.budget_after,
            degraded_mode=value.degraded_mode,
            created_at=value.created_at,
            run_id=value.run_id,
            step_id=value.step_id,
            profile_draft_id=value.profile_draft_id,
            plan_id=value.plan_id,
        )


class EntityVersionReferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: int

    @classmethod
    def from_domain(cls, value: EntityVersionReference) -> Self:
        return cls.model_validate(value)


class ContextSnapshotReferenceResponse(BaseModel):
    id: UUID
    agent_type: AgentType
    contract_version: str
    policy_version: str
    context_fingerprint: str
    context_audit_id: UUID
    profile_id: UUID | None
    profile_version: int | None
    constraint_versions: tuple[EntityVersionReferenceResponse, ...]
    memory_versions: tuple[EntityVersionReferenceResponse, ...]
    degraded_mode: ContextDegradedMode
    created_at: datetime

    @classmethod
    def from_domain(cls, value: ContextSnapshotReference) -> Self:
        return cls(
            id=value.id,
            agent_type=value.agent_type,
            contract_version=value.contract_version,
            policy_version=value.policy_version,
            context_fingerprint=value.context_fingerprint,
            context_audit_id=value.context_audit_id,
            profile_id=value.profile_id,
            profile_version=value.profile_version,
            constraint_versions=tuple(
                EntityVersionReferenceResponse.from_domain(item)
                for item in value.constraint_versions
            ),
            memory_versions=tuple(
                EntityVersionReferenceResponse.from_domain(item)
                for item in value.memory_versions
            ),
            degraded_mode=value.degraded_mode,
            created_at=value.created_at,
        )
