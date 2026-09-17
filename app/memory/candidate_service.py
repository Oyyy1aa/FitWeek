"""Explicit review lifecycle for inferred Memory Candidates."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.common import utc_now
from app.domain.memory.enums import (
    MemoryCandidateStatus,
    MemoryEvidenceType,
    MemorySource,
    MemoryStatus,
    MemoryType,
)
from app.domain.memory.errors import (
    MemoryCandidateNotFoundError,
    MemoryInvalidValueError,
)
from app.domain.memory.models import (
    CandidateReviewOutcome,
    CandidateWriteOutcome,
    MemoryCandidate,
    UserMemory,
)
from app.domain.memory.repositories import MemoryRepository
from app.memory.metrics import MemoryMetrics
from app.memory.normalization import fingerprint, normalize_key, normalize_value
from app.memory.service import MemoryService


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateCandidateCommand:
    client_request_id: str
    memory_type: MemoryType
    key: str
    value: str
    source: MemorySource
    source_reference: str
    evidence_summary: str
    confidence: Decimal | None
    expires_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptCandidateCommand:
    client_request_id: str
    expected_candidate_version: int
    confirmed_value: str
    valid_until: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class RejectCandidateCommand:
    client_request_id: str
    expected_candidate_version: int


class MemoryCandidateService:
    def __init__(
        self,
        repository: MemoryRepository,
        memory_service: MemoryService,
        metrics: MemoryMetrics,
    ) -> None:
        self._repository = repository
        self._memory_service = memory_service
        self._metrics = metrics

    async def create_candidate(
        self, user_id: UUID, command: CreateCandidateCommand
    ) -> CandidateWriteOutcome:
        now = utc_now()
        if command.expires_at <= now:
            raise MemoryInvalidValueError("Candidate expires_at must be in the future.")
        key = normalize_key(command.key)
        value = normalize_value(command.value)
        summary = command.evidence_summary.strip()
        if not summary or len(summary) > 240:
            raise MemoryInvalidValueError("evidence_summary must be a bounded summary.")
        payload_hash = fingerprint(
            {
                "user_id": str(user_id),
                "request_id": command.client_request_id.strip(),
                "type": command.memory_type.value,
                "key": key,
                "value": value,
                "source": command.source.value,
                "source_reference": command.source_reference.strip(),
                "expires_at": command.expires_at,
            }
        )
        candidate = MemoryCandidate(
            id=uuid5(
                NAMESPACE_URL, f"fitweek:memory-candidate:{user_id}:{payload_hash}"
            ),
            user_id=user_id,
            memory_type=command.memory_type,
            proposed_key=key,
            proposed_value=value,
            source=command.source,
            source_reference=command.source_reference.strip(),
            evidence_summary=summary,
            confidence=command.confidence,
            status=MemoryCandidateStatus.PENDING_REVIEW,
            created_at=now,
            expires_at=command.expires_at,
            reviewed_at=None,
            version=1,
        )
        outcome = await self._repository.create_candidate(
            client_request_id=command.client_request_id.strip(),
            payload_fingerprint=payload_hash,
            candidate=candidate,
        )
        if outcome.created:
            self._metrics.increment("memory_candidates_created")
        return outcome

    async def get_candidate(self, user_id: UUID, candidate_id: UUID) -> MemoryCandidate:
        candidate = await self._repository.get_candidate(
            user_id, candidate_id, utc_now()
        )
        if candidate is None:
            raise MemoryCandidateNotFoundError("Memory Candidate was not found.")
        return candidate

    async def list_candidates(self, user_id: UUID) -> list[MemoryCandidate]:
        return await self._repository.list_candidates(user_id, utc_now())

    async def accept_candidate(
        self,
        user_id: UUID,
        candidate_id: UUID,
        command: AcceptCandidateCommand,
    ) -> CandidateReviewOutcome:
        now = utc_now()
        candidate = await self.get_candidate(user_id, candidate_id)
        confirmed = normalize_value(command.confirmed_value)
        if confirmed != candidate.proposed_value:
            raise MemoryInvalidValueError(
                "confirmed_value must be normalization-equivalent to the Candidate."
            )
        self._memory_service._require_future(command.valid_until, now)
        payload_hash = fingerprint(
            {
                "user_id": str(user_id),
                "candidate_id": str(candidate_id),
                "expected_version": command.expected_candidate_version,
                "request_id": command.client_request_id.strip(),
                "confirmed_value": confirmed,
                "valid_until": command.valid_until,
            }
        )
        memory_id = uuid5(
            NAMESPACE_URL, f"fitweek:candidate-memory:{user_id}:{payload_hash}"
        )
        memory = UserMemory(
            id=memory_id,
            user_id=user_id,
            memory_type=candidate.memory_type,
            key=candidate.proposed_key,
            normalized_value=confirmed,
            display_value=command.confirmed_value.strip(),
            status=MemoryStatus.ACTIVE,
            source=candidate.source,
            confidence=candidate.confidence,
            valid_from=now,
            valid_until=command.valid_until,
            confirmed_at=now,
            created_at=now,
            updated_at=now,
            deleted_at=None,
            version=1,
        )
        evidence_type = (
            MemoryEvidenceType.PROFILE_DRAFT
            if candidate.source is MemorySource.PROFILE_AGENT_CANDIDATE
            else MemoryEvidenceType.CHECK_IN_SUMMARY
        )
        evidence = self._memory_service._evidence(
            memory_id=memory_id,
            evidence_type=evidence_type,
            source_reference=candidate.source_reference,
            memory_type=memory.memory_type,
            occurred_at=now,
        )
        outcome = await self._repository.accept_candidate(
            user_id=user_id,
            candidate_id=candidate_id,
            expected_version=command.expected_candidate_version,
            client_request_id=command.client_request_id.strip(),
            payload_fingerprint=payload_hash,
            confirmed_value=confirmed,
            memory=memory,
            evidence=evidence,
            now=now,
        )
        if outcome.created:
            self._metrics.increment("memory_candidates_accepted")
            if outcome.memory is not None:
                self._metrics.increment("memories_created")
                await self._memory_service._invalidate(user_id)
        return outcome

    async def reject_candidate(
        self,
        user_id: UUID,
        candidate_id: UUID,
        command: RejectCandidateCommand,
    ) -> CandidateReviewOutcome:
        now = utc_now()
        payload_hash = fingerprint(
            {
                "user_id": str(user_id),
                "candidate_id": str(candidate_id),
                "expected_version": command.expected_candidate_version,
                "request_id": command.client_request_id.strip(),
            }
        )
        outcome = await self._repository.reject_candidate(
            user_id=user_id,
            candidate_id=candidate_id,
            expected_version=command.expected_candidate_version,
            client_request_id=command.client_request_id.strip(),
            payload_fingerprint=payload_hash,
            now=now,
        )
        if outcome.created:
            self._metrics.increment("memory_candidates_rejected")
        return outcome
