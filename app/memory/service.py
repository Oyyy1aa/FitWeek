"""User-confirmed Memory CRUD, Evidence, version, and idempotency service."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from redis.exceptions import RedisError

from app.domain.common import utc_now
from app.domain.memory.enums import (
    MemoryEvidenceType,
    MemorySource,
    MemoryStatus,
    MemoryType,
)
from app.domain.memory.errors import MemoryNotFoundError
from app.domain.memory.models import (
    MemoryEvidence,
    MemoryRecord,
    MemoryReplaceOutcome,
    MemoryWriteOutcome,
    UserMemory,
)
from app.domain.memory.repositories import MemoryRepository
from app.memory.cache import MemoryActiveCache
from app.memory.metrics import MemoryMetrics
from app.memory.normalization import (
    fingerprint,
    normalize_display,
    normalize_key,
    normalize_value,
)
from app.memory.redaction import evidence_summary

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateMemoryCommand:
    client_request_id: str
    memory_type: MemoryType
    key: str
    value: str
    valid_until: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateMemoryCommand:
    expected_version: int
    value: str
    valid_until: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplaceMemoryCommand:
    client_request_id: str
    expected_version: int
    value: str
    valid_until: datetime | None


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        metrics: MemoryMetrics,
        cache: MemoryActiveCache | None = None,
    ) -> None:
        self._repository = repository
        self._metrics = metrics
        self._cache = cache

    @staticmethod
    def _require_future(expiration: datetime | None, now: datetime) -> None:
        if expiration is not None and expiration <= now:
            from app.domain.memory.errors import MemoryInvalidValueError

            raise MemoryInvalidValueError(
                "valid_until must be later than the current time."
            )

    @staticmethod
    def _evidence(
        *,
        memory_id: UUID,
        evidence_type: MemoryEvidenceType,
        source_reference: str,
        memory_type: MemoryType,
        occurred_at: datetime,
    ) -> MemoryEvidence:
        summary = evidence_summary(evidence_type, memory_type)
        content_hash = fingerprint(
            {
                "memory_id": str(memory_id),
                "evidence_type": evidence_type.value,
                "source_reference": source_reference,
                "summary": summary,
            }
        )
        return MemoryEvidence(
            id=uuid5(NAMESPACE_URL, f"fitweek:evidence:{content_hash}"),
            memory_id=memory_id,
            evidence_type=evidence_type,
            source_reference=source_reference,
            evidence_summary=summary,
            source_occurred_at=occurred_at,
            created_at=occurred_at,
            content_fingerprint=content_hash,
        )

    async def create_explicit_memory(
        self, user_id: UUID, command: CreateMemoryCommand
    ) -> MemoryWriteOutcome:
        now = utc_now()
        self._require_future(command.valid_until, now)
        key = normalize_key(command.key)
        display = normalize_display(command.value)
        normalized = normalize_value(command.value)
        payload_hash = fingerprint(
            {
                "user_id": str(user_id),
                "request_id": command.client_request_id.strip(),
                "type": command.memory_type.value,
                "key": key,
                "value": normalized,
                "valid_until": command.valid_until,
            }
        )
        memory_id = uuid5(NAMESPACE_URL, f"fitweek:memory:{user_id}:{payload_hash}")
        memory = UserMemory(
            id=memory_id,
            user_id=user_id,
            memory_type=command.memory_type,
            key=key,
            normalized_value=normalized,
            display_value=display,
            status=MemoryStatus.ACTIVE,
            source=MemorySource.USER_EXPLICIT,
            confidence=None,
            valid_from=now,
            valid_until=command.valid_until,
            confirmed_at=now,
            created_at=now,
            updated_at=now,
            deleted_at=None,
            version=1,
        )
        evidence = self._evidence(
            memory_id=memory_id,
            evidence_type=MemoryEvidenceType.USER_CONFIRMATION,
            source_reference=f"memory-request:{command.client_request_id.strip()}",
            memory_type=command.memory_type,
            occurred_at=now,
        )
        outcome = await self._repository.create_memory(
            client_request_id=command.client_request_id.strip(),
            payload_fingerprint=payload_hash,
            memory=memory,
            evidence=evidence,
        )
        if outcome.created:
            self._metrics.increment("memories_created")
            await self._invalidate(user_id)
        return outcome

    async def get_memory(self, user_id: UUID, memory_id: UUID) -> MemoryRecord:
        record = await self._repository.get_memory(user_id, memory_id, utc_now())
        if record is None:
            raise MemoryNotFoundError("Memory was not found.")
        return record

    async def list_memories(self, user_id: UUID) -> list[MemoryRecord]:
        return await self._repository.list_memories(user_id, utc_now())

    async def update_memory(
        self, user_id: UUID, memory_id: UUID, command: UpdateMemoryCommand
    ) -> MemoryRecord:
        now = utc_now()
        self._require_future(command.valid_until, now)
        current = await self.get_memory(user_id, memory_id)
        display = normalize_display(command.value)
        updated = current.memory.update_value(
            normalized_value=normalize_value(command.value),
            display_value=display,
            valid_until=command.valid_until,
            now=now,
        )
        evidence = self._evidence(
            memory_id=memory_id,
            evidence_type=MemoryEvidenceType.MANUAL_EDIT,
            source_reference=f"memory:{memory_id}:version:{command.expected_version}",
            memory_type=updated.memory_type,
            occurred_at=now,
        )
        saved = await self._repository.update_memory(
            user_id=user_id,
            memory=updated,
            expected_version=command.expected_version,
            evidence=evidence,
        )
        if saved is None:
            raise MemoryNotFoundError("Memory was not found.")
        self._metrics.increment("memories_updated")
        await self._invalidate(user_id)
        return saved

    async def delete_memory(
        self, user_id: UUID, memory_id: UUID, expected_version: int
    ) -> MemoryRecord:
        before = await self.get_memory(user_id, memory_id)
        saved = await self._repository.delete_memory(
            user_id=user_id,
            memory_id=memory_id,
            expected_version=expected_version,
            now=utc_now(),
        )
        if saved is None:
            raise MemoryNotFoundError("Memory was not found.")
        if saved.memory.status is not before.memory.status:
            self._metrics.increment("memories_deleted")
            await self._invalidate(user_id)
        return saved

    async def replace_memory(
        self, user_id: UUID, memory_id: UUID, command: ReplaceMemoryCommand
    ) -> MemoryReplaceOutcome:
        now = utc_now()
        self._require_future(command.valid_until, now)
        current = await self.get_memory(user_id, memory_id)
        display = normalize_display(command.value)
        normalized = normalize_value(command.value)
        payload_hash = fingerprint(
            {
                "user_id": str(user_id),
                "memory_id": str(memory_id),
                "expected_version": command.expected_version,
                "request_id": command.client_request_id.strip(),
                "value": normalized,
                "valid_until": command.valid_until,
            }
        )
        replacement_id = uuid5(
            NAMESPACE_URL, f"fitweek:memory-replacement:{user_id}:{payload_hash}"
        )
        replacement = UserMemory(
            id=replacement_id,
            user_id=user_id,
            memory_type=current.memory.memory_type,
            key=current.memory.key,
            normalized_value=normalized,
            display_value=display,
            status=MemoryStatus.ACTIVE,
            source=MemorySource.USER_EDIT,
            confidence=None,
            valid_from=now,
            valid_until=command.valid_until,
            confirmed_at=now,
            created_at=now,
            updated_at=now,
            deleted_at=None,
            version=1,
        )
        evidence = self._evidence(
            memory_id=replacement_id,
            evidence_type=MemoryEvidenceType.MANUAL_EDIT,
            source_reference=f"memory-replace:{command.client_request_id.strip()}",
            memory_type=replacement.memory_type,
            occurred_at=now,
        )
        outcome = await self._repository.replace_memory(
            user_id=user_id,
            memory_id=memory_id,
            expected_version=command.expected_version,
            client_request_id=command.client_request_id.strip(),
            payload_fingerprint=payload_hash,
            replacement=replacement,
            evidence=evidence,
            now=now,
        )
        if outcome is None:
            raise MemoryNotFoundError("Memory was not found.")
        if outcome.created:
            self._metrics.increment("memories_replaced")
            await self._invalidate(user_id)
        return outcome

    async def _invalidate(self, user_id: UUID) -> None:
        if self._cache is not None:
            try:
                await self._cache.invalidate_active(user_id)
            except (RedisError, OSError, TimeoutError):
                # The MySQL transaction has already committed; cache failure is lossy.
                self._metrics.increment("memory_cache_degraded")
                logger.warning(
                    "memory_cache_degraded", extra={"operation": "invalidate"}
                )

    def metrics(self) -> dict[str, int]:
        return self._metrics.snapshot().as_dict()
