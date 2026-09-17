"""Process-local idempotency adapter for Draft Candidate imports."""

from copy import deepcopy
from uuid import UUID

from app.domain.memory.errors import (
    DraftMemoryCandidateImportIdempotencyConflictError,
)
from app.domain.profile_agent.memory_candidates import (
    DraftMemoryCandidateImportResult,
)
from app.persistence.memory.store import InMemoryStore


class InMemoryDraftMemoryCandidateImportRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get(
        self, user_id: UUID, client_request_id: str
    ) -> tuple[str, DraftMemoryCandidateImportResult] | None:
        async with self._store.lock:
            value = self._store._draft_memory_candidate_imports.get(
                (user_id, client_request_id)
            )
            return deepcopy(value)

    async def save(
        self,
        user_id: UUID,
        client_request_id: str,
        fingerprint: str,
        result: DraftMemoryCandidateImportResult,
    ) -> DraftMemoryCandidateImportResult:
        key = (user_id, client_request_id)
        async with self._store.lock:
            existing = self._store._draft_memory_candidate_imports.get(key)
            if existing is not None:
                if existing[0] != fingerprint:
                    raise DraftMemoryCandidateImportIdempotencyConflictError(
                        "The import request ID was used with another selection."
                    )
                return deepcopy(existing[1])
            self._store._draft_memory_candidate_imports[key] = (
                fingerprint,
                deepcopy(result),
            )
            return deepcopy(result)
