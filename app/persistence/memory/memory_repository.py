"""Deep-copying, user-isolated, atomic in-memory Memory adapter."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from uuid import UUID

from app.domain.context.models import ContextBuildAudit
from app.domain.memory.enums import MemoryCandidateStatus, MemoryStatus
from app.domain.memory.errors import (
    MemoryCandidateAlreadyAcceptedError,
    MemoryCandidateExpiredError,
    MemoryCandidateIdempotencyConflictError,
    MemoryCandidateNotFoundError,
    MemoryCandidateRejectedError,
    MemoryCandidateVersionConflictError,
    MemoryConflictError,
    MemoryIdempotencyConflictError,
    MemoryNotActiveError,
    MemoryQueryFailedError,
    MemoryVersionConflictError,
)
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
from app.domain.memory.policies import SINGLE_VALUE_MEMORY_TYPES
from app.persistence.memory.store import InMemoryStore


class InMemoryMemoryRepository:
    """Store Memory state under the application-scoped shared lock."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def set_query_failures(self, count: int) -> None:
        """Test-only fault injection used by the real loopback HTTP harness."""

        if count < 0:
            raise ValueError("query failure count cannot be negative")
        self._store._memory_query_failures_remaining = count

    def _record_unlocked(self, memory_id: UUID) -> MemoryRecord:
        memory = self._store._memories[memory_id]
        evidence = tuple(self._store._memory_evidence.get(memory_id, ()))
        return deepcopy(MemoryRecord(memory=memory, evidence=evidence))

    def _remove_active_indexes_unlocked(self, memory: UserMemory) -> None:
        exact_key = (
            memory.user_id,
            memory.memory_type,
            memory.key,
            memory.normalized_value,
        )
        if self._store._active_memory_exact.get(exact_key) == memory.id:
            self._store._active_memory_exact.pop(exact_key, None)
        single_key = (memory.user_id, memory.memory_type, memory.key)
        if self._store._active_memory_single.get(single_key) == memory.id:
            self._store._active_memory_single.pop(single_key, None)

    def _add_active_indexes_unlocked(self, memory: UserMemory) -> None:
        self._store._active_memory_exact[
            (memory.user_id, memory.memory_type, memory.key, memory.normalized_value)
        ] = memory.id
        if memory.memory_type in SINGLE_VALUE_MEMORY_TYPES:
            self._store._active_memory_single[
                (memory.user_id, memory.memory_type, memory.key)
            ] = memory.id

    def _expire_memory_unlocked(self, memory_id: UUID, now: datetime) -> UserMemory:
        memory = self._store._memories[memory_id]
        if (
            memory.status is MemoryStatus.ACTIVE
            and memory.valid_until is not None
            and memory.valid_until <= now
        ):
            self._remove_active_indexes_unlocked(memory)
            memory = memory.expire(now)
            self._store._memories[memory_id] = memory
        return memory

    def _expire_candidate_unlocked(
        self, candidate_id: UUID, now: datetime
    ) -> MemoryCandidate:
        candidate = self._store._memory_candidates[candidate_id]
        if (
            candidate.status is MemoryCandidateStatus.PENDING_REVIEW
            and candidate.expires_at <= now
        ):
            candidate = candidate.expire()
            self._store._memory_candidates[candidate_id] = candidate
        return candidate

    def _find_duplicate_or_conflict_unlocked(self, memory: UserMemory) -> UUID | None:
        exact = self._store._active_memory_exact.get(
            (
                memory.user_id,
                memory.memory_type,
                memory.key,
                memory.normalized_value,
            )
        )
        if exact is not None:
            return exact
        if memory.memory_type in SINGLE_VALUE_MEMORY_TYPES:
            conflict = self._store._active_memory_single.get(
                (memory.user_id, memory.memory_type, memory.key)
            )
            if conflict is not None:
                raise MemoryConflictError(
                    "A conflicting ACTIVE single-value Memory already exists; "
                    "use replace."
                )
        return None

    async def create_memory(
        self,
        *,
        client_request_id: str,
        payload_fingerprint: str,
        memory: UserMemory,
        evidence: MemoryEvidence,
    ) -> MemoryWriteOutcome:
        request_key = (memory.user_id, "create", client_request_id)
        async with self._store.lock:
            existing_request = self._store._memory_request_index.get(request_key)
            if existing_request is not None:
                if existing_request[0] != payload_fingerprint:
                    raise MemoryIdempotencyConflictError(
                        "The Memory idempotency key was reused with a different "
                        "payload."
                    )
                return MemoryWriteOutcome(
                    record=self._record_unlocked(existing_request[1]), created=False
                )
            duplicate_id = self._find_duplicate_or_conflict_unlocked(memory)
            if duplicate_id is not None:
                self._store._memory_request_index[request_key] = (
                    payload_fingerprint,
                    duplicate_id,
                    None,
                )
                return MemoryWriteOutcome(
                    record=self._record_unlocked(duplicate_id), created=False
                )
            self._store.require_first_version("memory", memory.id, memory.version)
            self._store._memories[memory.id] = deepcopy(memory)
            self._store._memory_evidence[memory.id] = [deepcopy(evidence)]
            self._add_active_indexes_unlocked(memory)
            self._store._memory_request_index[request_key] = (
                payload_fingerprint,
                memory.id,
                None,
            )
            return MemoryWriteOutcome(
                record=self._record_unlocked(memory.id), created=True
            )

    async def get_memory(
        self, user_id: UUID, memory_id: UUID, now: datetime
    ) -> MemoryRecord | None:
        async with self._store.lock:
            memory = self._store._memories.get(memory_id)
            if memory is None or memory.user_id != user_id:
                return None
            self._expire_memory_unlocked(memory_id, now)
            return self._record_unlocked(memory_id)

    async def list_memories(self, user_id: UUID, now: datetime) -> list[MemoryRecord]:
        async with self._store.lock:
            ids = [
                item.id
                for item in self._store._memories.values()
                if item.user_id == user_id
            ]
            for memory_id in ids:
                self._expire_memory_unlocked(memory_id, now)
            return [
                self._record_unlocked(memory_id)
                for memory_id in sorted(
                    ids,
                    key=lambda item_id: (
                        self._store._memories[item_id].created_at,
                        str(item_id),
                    ),
                )
            ]

    async def update_memory(
        self,
        *,
        user_id: UUID,
        memory: UserMemory,
        expected_version: int,
        evidence: MemoryEvidence,
    ) -> MemoryRecord | None:
        async with self._store.lock:
            current = self._store._memories.get(memory.id)
            if current is None or current.user_id != user_id:
                return None
            if current.version != expected_version:
                raise MemoryVersionConflictError("Memory version does not match.")
            if current.status is not MemoryStatus.ACTIVE:
                raise MemoryNotActiveError("Only ACTIVE Memory can be updated.")
            self._store.require_next_version(
                "memory",
                memory.id,
                current_version=current.version,
                incoming_version=memory.version,
            )
            self._remove_active_indexes_unlocked(current)
            try:
                duplicate = self._find_duplicate_or_conflict_unlocked(memory)
                if duplicate is not None and duplicate != memory.id:
                    raise MemoryConflictError(
                        "An equivalent ACTIVE Memory already exists."
                    )
            except Exception:
                self._add_active_indexes_unlocked(current)
                raise
            self._store._memories[memory.id] = deepcopy(memory)
            self._store._memory_evidence[memory.id].append(deepcopy(evidence))
            self._add_active_indexes_unlocked(memory)
            return self._record_unlocked(memory.id)

    async def delete_memory(
        self,
        *,
        user_id: UUID,
        memory_id: UUID,
        expected_version: int,
        now: datetime,
    ) -> MemoryRecord | None:
        async with self._store.lock:
            current = self._store._memories.get(memory_id)
            if current is None or current.user_id != user_id:
                return None
            if current.status is MemoryStatus.DELETED:
                return self._record_unlocked(memory_id)
            if current.version != expected_version:
                raise MemoryVersionConflictError("Memory version does not match.")
            if current.status is not MemoryStatus.ACTIVE:
                raise MemoryNotActiveError("Only ACTIVE Memory can be deleted.")
            self._remove_active_indexes_unlocked(current)
            self._store._memories[memory_id] = current.delete(now)
            return self._record_unlocked(memory_id)

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
    ) -> MemoryReplaceOutcome | None:
        request_key = (user_id, "replace", client_request_id)
        async with self._store.lock:
            replay = self._store._memory_request_index.get(request_key)
            if replay is not None:
                if replay[0] != payload_fingerprint:
                    raise MemoryIdempotencyConflictError(
                        "The replace idempotency key was reused with another payload."
                    )
                if replay[2] is None:
                    raise MemoryConflictError(
                        "Stored replacement reference is incomplete."
                    )
                return MemoryReplaceOutcome(
                    previous=self._record_unlocked(replay[1]),
                    replacement=self._record_unlocked(replay[2]),
                    created=False,
                )
            current = self._store._memories.get(memory_id)
            if current is None or current.user_id != user_id:
                return None
            if current.version != expected_version:
                raise MemoryVersionConflictError("Memory version does not match.")
            if current.status is not MemoryStatus.ACTIVE:
                raise MemoryNotActiveError("Only ACTIVE Memory can be replaced.")
            self._store.require_first_version(
                "memory", replacement.id, replacement.version
            )
            self._remove_active_indexes_unlocked(current)
            try:
                duplicate = self._find_duplicate_or_conflict_unlocked(replacement)
                if duplicate is not None:
                    raise MemoryConflictError(
                        "Replacement already exists as ACTIVE Memory."
                    )
            except Exception:
                self._add_active_indexes_unlocked(current)
                raise
            superseded = current.supersede(now)
            self._store._memories[current.id] = superseded
            self._store._memories[replacement.id] = deepcopy(replacement)
            self._store._memory_evidence[replacement.id] = [deepcopy(evidence)]
            self._add_active_indexes_unlocked(replacement)
            self._store._memory_request_index[request_key] = (
                payload_fingerprint,
                current.id,
                replacement.id,
            )
            return MemoryReplaceOutcome(
                previous=self._record_unlocked(current.id),
                replacement=self._record_unlocked(replacement.id),
                created=True,
            )

    async def create_candidate(
        self,
        *,
        client_request_id: str,
        payload_fingerprint: str,
        candidate: MemoryCandidate,
    ) -> CandidateWriteOutcome:
        request_key = (candidate.user_id, "candidate-create", client_request_id)
        content_key = (
            candidate.user_id,
            candidate.source_reference,
            candidate.memory_type,
            candidate.proposed_key,
            candidate.proposed_value,
        )
        async with self._store.lock:
            replay = self._store._candidate_request_index.get(request_key)
            if replay is not None:
                if replay[0] != payload_fingerprint:
                    raise MemoryCandidateIdempotencyConflictError(
                        "Candidate idempotency key was reused with another payload."
                    )
                return CandidateWriteOutcome(
                    candidate=deepcopy(self._store._memory_candidates[replay[1]]),
                    created=False,
                )
            duplicate = self._store._candidate_by_source_content.get(content_key)
            if duplicate is not None:
                self._store._candidate_request_index[request_key] = (
                    payload_fingerprint,
                    duplicate,
                    None,
                )
                return CandidateWriteOutcome(
                    candidate=deepcopy(self._store._memory_candidates[duplicate]),
                    created=False,
                )
            self._store.require_first_version(
                "memory_candidate", candidate.id, candidate.version
            )
            self._store._memory_candidates[candidate.id] = deepcopy(candidate)
            self._store._candidate_by_source_content[content_key] = candidate.id
            self._store._candidate_request_index[request_key] = (
                payload_fingerprint,
                candidate.id,
                None,
            )
            return CandidateWriteOutcome(candidate=deepcopy(candidate), created=True)

    async def get_candidate(
        self, user_id: UUID, candidate_id: UUID, now: datetime
    ) -> MemoryCandidate | None:
        async with self._store.lock:
            candidate = self._store._memory_candidates.get(candidate_id)
            if candidate is None or candidate.user_id != user_id:
                return None
            return deepcopy(self._expire_candidate_unlocked(candidate_id, now))

    async def list_candidates(
        self, user_id: UUID, now: datetime
    ) -> list[MemoryCandidate]:
        async with self._store.lock:
            ids = [
                item.id
                for item in self._store._memory_candidates.values()
                if item.user_id == user_id
            ]
            values = [self._expire_candidate_unlocked(item, now) for item in ids]
            return deepcopy(
                sorted(values, key=lambda item: (item.created_at, str(item.id)))
            )

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
    ) -> CandidateReviewOutcome:
        request_key = (user_id, "candidate-accept", client_request_id)
        async with self._store.lock:
            replay = self._store._candidate_request_index.get(request_key)
            if replay is not None:
                if replay[0] != payload_fingerprint:
                    raise MemoryCandidateIdempotencyConflictError(
                        "Candidate accept key was reused with another payload."
                    )
                return CandidateReviewOutcome(
                    candidate=deepcopy(self._store._memory_candidates[replay[1]]),
                    memory=(self._record_unlocked(replay[2]) if replay[2] else None),
                    created=False,
                )
            candidate = self._store._memory_candidates.get(candidate_id)
            if candidate is None or candidate.user_id != user_id:
                raise MemoryCandidateNotFoundError("Memory Candidate was not found.")
            candidate = self._expire_candidate_unlocked(candidate_id, now)
            if candidate.status is MemoryCandidateStatus.EXPIRED:
                raise MemoryCandidateExpiredError("Memory Candidate has expired.")
            if candidate.status is MemoryCandidateStatus.REJECTED:
                raise MemoryCandidateRejectedError(
                    "Rejected Candidate cannot be accepted."
                )
            if candidate.status is MemoryCandidateStatus.ACCEPTED:
                raise MemoryCandidateAlreadyAcceptedError(
                    "Candidate is already accepted."
                )
            if candidate.version != expected_version:
                raise MemoryCandidateVersionConflictError(
                    "Memory Candidate version does not match."
                )
            if confirmed_value != candidate.proposed_value:
                raise MemoryConflictError(
                    "Confirmed value must be normalization-equivalent to the Candidate."
                )
            duplicate_id = self._find_duplicate_or_conflict_unlocked(memory)
            if duplicate_id is None:
                self._store._memories[memory.id] = deepcopy(memory)
                self._store._memory_evidence[memory.id] = [deepcopy(evidence)]
                self._add_active_indexes_unlocked(memory)
                memory_id = memory.id
            else:
                memory_id = duplicate_id
            accepted = candidate.accept(now)
            self._store._memory_candidates[candidate_id] = accepted
            self._store._candidate_request_index[request_key] = (
                payload_fingerprint,
                candidate_id,
                memory_id,
            )
            return CandidateReviewOutcome(
                candidate=deepcopy(accepted),
                memory=self._record_unlocked(memory_id),
                created=True,
            )

    async def reject_candidate(
        self,
        *,
        user_id: UUID,
        candidate_id: UUID,
        expected_version: int,
        client_request_id: str,
        payload_fingerprint: str,
        now: datetime,
    ) -> CandidateReviewOutcome:
        request_key = (user_id, "candidate-reject", client_request_id)
        async with self._store.lock:
            replay = self._store._candidate_request_index.get(request_key)
            if replay is not None:
                if replay[0] != payload_fingerprint:
                    raise MemoryCandidateIdempotencyConflictError(
                        "Candidate reject key was reused with another payload."
                    )
                return CandidateReviewOutcome(
                    candidate=deepcopy(self._store._memory_candidates[replay[1]]),
                    memory=None,
                    created=False,
                )
            candidate = self._store._memory_candidates.get(candidate_id)
            if candidate is None or candidate.user_id != user_id:
                raise MemoryCandidateNotFoundError("Memory Candidate was not found.")
            candidate = self._expire_candidate_unlocked(candidate_id, now)
            if candidate.status is MemoryCandidateStatus.EXPIRED:
                raise MemoryCandidateExpiredError("Memory Candidate has expired.")
            if candidate.status is MemoryCandidateStatus.ACCEPTED:
                raise MemoryCandidateAlreadyAcceptedError(
                    "Accepted Candidate cannot be rejected."
                )
            if candidate.status is MemoryCandidateStatus.REJECTED:
                raise MemoryCandidateRejectedError(
                    "Candidate was rejected by another request."
                )
            if candidate.version != expected_version:
                raise MemoryCandidateVersionConflictError(
                    "Memory Candidate version does not match."
                )
            rejected = candidate.reject(now)
            self._store._memory_candidates[candidate_id] = rejected
            self._store._candidate_request_index[request_key] = (
                payload_fingerprint,
                candidate_id,
                None,
            )
            return CandidateReviewOutcome(
                candidate=deepcopy(rejected), memory=None, created=True
            )

    async def list_active_for_context(
        self, user_id: UUID, now: datetime
    ) -> MemoryQueryResult:
        async with self._store.lock:
            if self._store._memory_query_failures_remaining > 0:
                self._store._memory_query_failures_remaining -= 1
                raise MemoryQueryFailedError("Injected in-memory Memory query failure.")
            owned_ids = [
                memory.id
                for memory in self._store._memories.values()
                if memory.user_id == user_id
            ]
            for memory_id in owned_ids:
                self._expire_memory_unlocked(memory_id, now)
            owned = [self._store._memories[item] for item in owned_ids]
            active = tuple(
                deepcopy(
                    sorted(
                        (item for item in owned if item.status is MemoryStatus.ACTIVE),
                        key=lambda item: (
                            -(
                                item.confirmed_at.timestamp()
                                if item.confirmed_at
                                else 0
                            ),
                            item.memory_type.value,
                            item.key,
                            str(item.id),
                        ),
                    )
                )
            )
            return MemoryQueryResult(
                memories=active,
                expired_filtered=sum(
                    item.status is MemoryStatus.EXPIRED for item in owned
                ),
                deleted_filtered=sum(
                    item.status is MemoryStatus.DELETED for item in owned
                ),
                pending_filtered=sum(
                    item.status is MemoryStatus.PENDING_CONFIRMATION for item in owned
                ),
                filtered_reasons={
                    item.id: item.status.value
                    for item in owned
                    if item.status is not MemoryStatus.ACTIVE
                },
            )

    async def save_context_audit(self, audit: ContextBuildAudit) -> ContextBuildAudit:
        async with self._store.lock:
            existing = self._store._context_audits.get(audit.id)
            if existing is not None and existing != audit:
                raise MemoryConflictError("Context Audit ID already exists.")
            self._store._context_audits[audit.id] = deepcopy(audit)
            return deepcopy(audit)

    async def get_context_audit(
        self, user_id: UUID, audit_id: UUID
    ) -> ContextBuildAudit | None:
        async with self._store.lock:
            audit = self._store._context_audits.get(audit_id)
            if audit is None or audit.user_id != user_id:
                return None
            return deepcopy(audit)

    async def reset(self) -> None:
        await self._store.reset()
