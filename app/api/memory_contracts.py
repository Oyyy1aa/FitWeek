"""Strict Phase 4A Memory and Candidate HTTP contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.contracts import RequestModel
from app.domain.memory.enums import (
    MemoryCandidateStatus,
    MemoryEvidenceType,
    MemorySource,
    MemoryStatus,
    MemoryType,
)
from app.domain.memory.models import (
    CandidateReviewOutcome,
    MemoryCandidate,
    MemoryEvidence,
    MemoryRecord,
    MemoryReplaceOutcome,
)


class CreateMemoryRequest(RequestModel):
    client_request_id: str = Field(min_length=1, max_length=120)
    memory_type: MemoryType
    key: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=240)
    valid_until: datetime | None = None


class UpdateMemoryRequest(RequestModel):
    expected_version: int = Field(ge=1)
    value: str = Field(min_length=1, max_length=240)
    valid_until: datetime | None = None


class DeleteMemoryRequest(RequestModel):
    expected_version: int = Field(ge=1)


class ReplaceMemoryRequest(RequestModel):
    client_request_id: str = Field(min_length=1, max_length=120)
    expected_version: int = Field(ge=1)
    value: str = Field(min_length=1, max_length=240)
    valid_until: datetime | None = None


class MemoryEvidenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    evidence_type: MemoryEvidenceType
    source_reference: str
    evidence_summary: str
    source_occurred_at: datetime
    created_at: datetime
    content_fingerprint: str

    @classmethod
    def from_domain(cls, value: MemoryEvidence) -> Self:
        return cls.model_validate(value)


class MemoryResponse(BaseModel):
    id: UUID
    memory_type: MemoryType
    key: str
    normalized_value: str
    display_value: str
    status: MemoryStatus
    source: MemorySource
    confidence: Decimal | None
    valid_from: datetime
    valid_until: datetime | None
    confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    version: int
    evidence: tuple[MemoryEvidenceResponse, ...]

    @classmethod
    def from_domain(cls, value: MemoryRecord) -> Self:
        memory = value.memory
        return cls(
            **{
                field: getattr(memory, field)
                for field in cls.model_fields
                if field != "evidence"
            },
            evidence=tuple(
                MemoryEvidenceResponse.from_domain(item) for item in value.evidence
            ),
        )


class ReplaceMemoryResponse(BaseModel):
    previous: MemoryResponse
    replacement: MemoryResponse
    created: bool

    @classmethod
    def from_domain(cls, value: MemoryReplaceOutcome) -> Self:
        return cls(
            previous=MemoryResponse.from_domain(value.previous),
            replacement=MemoryResponse.from_domain(value.replacement),
            created=value.created,
        )


class CreateMemoryCandidateRequest(RequestModel):
    client_request_id: str = Field(min_length=1, max_length=120)
    memory_type: MemoryType
    key: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=240)
    source: MemorySource
    source_reference: str = Field(min_length=1, max_length=160)
    evidence_summary: str = Field(min_length=1, max_length=240)
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    expires_at: datetime

    @field_validator("source")
    @classmethod
    def require_candidate_source(cls, value: MemorySource) -> MemorySource:
        if value not in {
            MemorySource.PROFILE_AGENT_CANDIDATE,
            MemorySource.BEHAVIOR_CANDIDATE,
        }:
            raise ValueError("development Candidate must use an inference source")
        return value


class AcceptMemoryCandidateRequest(RequestModel):
    client_request_id: str = Field(min_length=1, max_length=120)
    expected_candidate_version: int = Field(ge=1)
    confirmed_value: str = Field(min_length=1, max_length=240)
    valid_until: datetime | None = None


class RejectMemoryCandidateRequest(RequestModel):
    client_request_id: str = Field(min_length=1, max_length=120)
    expected_candidate_version: int = Field(ge=1)


class MemoryCandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    memory_type: MemoryType
    proposed_key: str
    proposed_value: str
    source: MemorySource
    source_reference: str
    evidence_summary: str
    confidence: Decimal | None
    status: MemoryCandidateStatus
    created_at: datetime
    expires_at: datetime
    reviewed_at: datetime | None
    version: int

    @classmethod
    def from_domain(cls, value: MemoryCandidate) -> Self:
        return cls.model_validate(value)


class CandidateReviewResponse(BaseModel):
    candidate: MemoryCandidateResponse
    memory: MemoryResponse | None
    created: bool

    @classmethod
    def from_domain(cls, value: CandidateReviewOutcome) -> Self:
        return cls(
            candidate=MemoryCandidateResponse.from_domain(value.candidate),
            memory=(MemoryResponse.from_domain(value.memory) if value.memory else None),
            created=value.created,
        )


class MemoryMetricsResponse(BaseModel):
    memories_created: int
    memories_updated: int
    memories_deleted: int
    memories_replaced: int
    memory_candidates_created: int
    memory_candidates_accepted: int
    memory_candidates_rejected: int
    memory_candidates_expired: int
    memory_queries: int
    memory_query_failures: int
    context_builds: int
    context_build_failures: int
    context_no_memory_degraded: int
    expired_memories_filtered: int
    deleted_memories_filtered: int
    pending_memories_filtered: int
    memory_conflicts_resolved: int
    context_budget_rejections: int
    profile_agent_context_builds: int
    plan_generation_context_builds: int
    context_snapshots_created: int
    context_snapshots_reused: int
    context_snapshot_failures: int
    profile_agent_no_memory_degraded: int
    plan_generation_no_memory_degraded: int
    draft_memory_candidate_previews: int
    draft_memory_candidates_imported: int
    draft_memory_candidate_import_conflicts: int
    memories_injected_into_profile_agent: int
    memories_injected_into_plan_generation: int
    memories_shadowed_by_current_task: int
    memories_shadowed_by_constraints: int
    memory_prompt_injection_cases_safely_encoded: int
    memory_cache_degraded: int
