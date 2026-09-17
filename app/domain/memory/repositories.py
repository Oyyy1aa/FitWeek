"""Memory repository port; adapters own atomicity, not business relevance."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.context.models import ContextBuildAudit
from app.domain.memory.models import (
    CandidateReviewOutcome,
    CandidateWriteOutcome,
    MemoryCandidate,
    MemoryEvidence,
    MemoryQueryResult,
    MemoryRecord,
    MemoryReplaceOutcome,
    MemoryWriteOutcome,
    UserMemory,
)


class MemoryRepository(Protocol):
    async def create_memory(
        self,
        *,
        client_request_id: str,
        payload_fingerprint: str,
        memory: UserMemory,
        evidence: MemoryEvidence,
    ) -> MemoryWriteOutcome: ...

    async def get_memory(
        self, user_id: UUID, memory_id: UUID, now: datetime
    ) -> MemoryRecord | None: ...

    async def list_memories(
        self, user_id: UUID, now: datetime
    ) -> list[MemoryRecord]: ...

    async def update_memory(
        self,
        *,
        user_id: UUID,
        memory: UserMemory,
        expected_version: int,
        evidence: MemoryEvidence,
    ) -> MemoryRecord | None: ...

    async def delete_memory(
        self,
        *,
        user_id: UUID,
        memory_id: UUID,
        expected_version: int,
        now: datetime,
    ) -> MemoryRecord | None: ...

    async def replace_memory(
        self,
        *,
        user_id: UUID,
        memory_id: UUID,
        expected_version: int,
        client_request_id: str,
        payload_fingerprint: str,
        replacement: UserMemory,
        evidence: MemoryEvidence,
        now: datetime,
    ) -> MemoryReplaceOutcome | None: ...

    async def create_candidate(
        self,
        *,
        client_request_id: str,
        payload_fingerprint: str,
        candidate: MemoryCandidate,
    ) -> CandidateWriteOutcome: ...

    async def get_candidate(
        self, user_id: UUID, candidate_id: UUID, now: datetime
    ) -> MemoryCandidate | None: ...

    async def list_candidates(
        self, user_id: UUID, now: datetime
    ) -> list[MemoryCandidate]: ...

    async def accept_candidate(
        self,
        *,
        user_id: UUID,
        candidate_id: UUID,
        expected_version: int,
        client_request_id: str,
        payload_fingerprint: str,
        confirmed_value: str,
        memory: UserMemory,
        evidence: MemoryEvidence,
        now: datetime,
    ) -> CandidateReviewOutcome: ...

    async def reject_candidate(
        self,
        *,
        user_id: UUID,
        candidate_id: UUID,
        expected_version: int,
        client_request_id: str,
        payload_fingerprint: str,
        now: datetime,
    ) -> CandidateReviewOutcome: ...

    async def list_active_for_context(
        self, user_id: UUID, now: datetime
    ) -> MemoryQueryResult: ...

    async def save_context_audit(
        self, audit: ContextBuildAudit
    ) -> ContextBuildAudit: ...

    async def get_context_audit(
        self, user_id: UUID, audit_id: UUID
    ) -> ContextBuildAudit | None: ...

    async def reset(self) -> None: ...
