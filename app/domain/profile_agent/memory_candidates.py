"""Explicit Profile Draft proposal-to-Memory Candidate review contracts."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.domain.memory.enums import MemoryCandidateStatus, MemoryType


@dataclass(frozen=True, slots=True, kw_only=True)
class DraftMemoryCandidatePreviewItem:
    proposal_index: int
    memory_type: MemoryType | None
    proposed_key: str | None
    proposed_value: str
    source_reference: str
    evidence_summary: str
    supported: bool
    conflict: bool = False
    duplicate: bool = False
    rejection_reason: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DraftMemoryCandidatePreview:
    draft_id: UUID
    draft_version: int
    items: tuple[DraftMemoryCandidatePreviewItem, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportDraftMemoryCandidatesCommand:
    client_request_id: str
    expected_draft_version: int
    selected_candidate_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class DraftMemoryCandidateImportResult:
    draft_id: UUID
    client_request_id: str
    import_fingerprint: str
    candidate_ids: tuple[UUID, ...]
    statuses: tuple[MemoryCandidateStatus, ...]
    created: bool


class DraftMemoryCandidateImportRepository(Protocol):
    async def get(
        self, user_id: UUID, client_request_id: str
    ) -> tuple[str, DraftMemoryCandidateImportResult] | None: ...

    async def save(
        self,
        user_id: UUID,
        client_request_id: str,
        fingerprint: str,
        result: DraftMemoryCandidateImportResult,
    ) -> DraftMemoryCandidateImportResult: ...
