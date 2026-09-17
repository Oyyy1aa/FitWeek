"""Immutable Memory, Evidence, Candidate, and repository result values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.domain.common import (
    DomainValidationError,
    require_non_blank,
    require_utc_datetime,
    require_version,
)
from app.domain.memory.enums import (
    MemoryCandidateStatus,
    MemoryEvidenceType,
    MemorySource,
    MemoryStatus,
    MemoryType,
)


def _require_uuid(value: UUID, field_name: str) -> None:
    if not isinstance(value, UUID):
        raise DomainValidationError(f"{field_name} must be a UUID.")


def _require_bounded_text(value: str, field_name: str, maximum: int) -> None:
    require_non_blank(value, field_name)
    if len(value) > maximum:
        raise DomainValidationError(
            f"{field_name} must not exceed {maximum} characters."
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryEvidence:
    id: UUID
    memory_id: UUID
    evidence_type: MemoryEvidenceType
    source_reference: str
    evidence_summary: str
    source_occurred_at: datetime
    created_at: datetime
    content_fingerprint: str

    def __post_init__(self) -> None:
        _require_uuid(self.id, "id")
        _require_uuid(self.memory_id, "memory_id")
        if not isinstance(self.evidence_type, MemoryEvidenceType):
            raise DomainValidationError("evidence_type must be controlled.")
        _require_bounded_text(self.source_reference, "source_reference", 160)
        _require_bounded_text(self.evidence_summary, "evidence_summary", 240)
        require_utc_datetime(self.source_occurred_at, "source_occurred_at")
        require_utc_datetime(self.created_at, "created_at")
        if len(self.content_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.content_fingerprint
        ):
            raise DomainValidationError(
                "content_fingerprint must be lowercase SHA-256."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class UserMemory:
    id: UUID
    user_id: UUID
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

    def __post_init__(self) -> None:
        _require_uuid(self.id, "id")
        _require_uuid(self.user_id, "user_id")
        if not isinstance(self.memory_type, MemoryType):
            raise DomainValidationError("memory_type must be controlled.")
        if not isinstance(self.status, MemoryStatus):
            raise DomainValidationError("status must be controlled.")
        if not isinstance(self.source, MemorySource):
            raise DomainValidationError("source must be controlled.")
        _require_bounded_text(self.key, "key", 80)
        _require_bounded_text(self.normalized_value, "normalized_value", 120)
        _require_bounded_text(self.display_value, "display_value", 240)
        if self.confidence is not None and not Decimal(
            "0"
        ) <= self.confidence <= Decimal("1"):
            raise DomainValidationError("confidence must be between 0 and 1.")
        for field_name in ("valid_from", "created_at", "updated_at"):
            require_utc_datetime(getattr(self, field_name), field_name)
        if self.valid_until is not None:
            require_utc_datetime(self.valid_until, "valid_until")
            if self.valid_until <= self.valid_from:
                raise DomainValidationError(
                    "valid_until must be later than valid_from."
                )
        if self.updated_at < self.created_at:
            raise DomainValidationError("updated_at cannot precede created_at.")
        if self.confirmed_at is not None:
            require_utc_datetime(self.confirmed_at, "confirmed_at")
        if self.deleted_at is not None:
            require_utc_datetime(self.deleted_at, "deleted_at")
        if self.status is MemoryStatus.ACTIVE and self.confirmed_at is None:
            raise DomainValidationError("ACTIVE Memory requires confirmed_at.")
        if self.status is MemoryStatus.DELETED and self.deleted_at is None:
            raise DomainValidationError("DELETED Memory requires deleted_at.")
        if self.status is not MemoryStatus.DELETED and self.deleted_at is not None:
            raise DomainValidationError("only DELETED Memory can contain deleted_at.")
        if self.source is MemorySource.USER_EXPLICIT and self.confidence is not None:
            raise DomainValidationError("USER_EXPLICIT Memory does not use confidence.")
        require_version(self.version)

    def expire(self, now: datetime) -> UserMemory:
        require_utc_datetime(now, "now")
        if self.status is not MemoryStatus.ACTIVE:
            return self
        return replace(
            self, status=MemoryStatus.EXPIRED, updated_at=now, version=self.version + 1
        )

    def delete(self, now: datetime) -> UserMemory:
        require_utc_datetime(now, "now")
        if self.status is MemoryStatus.DELETED:
            return self
        if self.status is not MemoryStatus.ACTIVE:
            raise DomainValidationError("only ACTIVE Memory can be deleted.")
        return replace(
            self,
            status=MemoryStatus.DELETED,
            deleted_at=now,
            updated_at=now,
            version=self.version + 1,
        )

    def supersede(self, now: datetime) -> UserMemory:
        require_utc_datetime(now, "now")
        if self.status is not MemoryStatus.ACTIVE:
            raise DomainValidationError("only ACTIVE Memory can be superseded.")
        return replace(
            self,
            status=MemoryStatus.SUPERSEDED,
            updated_at=now,
            version=self.version + 1,
        )

    def update_value(
        self,
        *,
        normalized_value: str,
        display_value: str,
        valid_until: datetime | None,
        now: datetime,
    ) -> UserMemory:
        if self.status is not MemoryStatus.ACTIVE:
            raise DomainValidationError("only ACTIVE Memory can be updated.")
        return replace(
            self,
            normalized_value=normalized_value,
            display_value=display_value,
            valid_until=valid_until,
            source=MemorySource.USER_EDIT,
            confidence=None,
            updated_at=now,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryCandidate:
    id: UUID
    user_id: UUID
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

    def __post_init__(self) -> None:
        _require_uuid(self.id, "id")
        _require_uuid(self.user_id, "user_id")
        if not isinstance(self.memory_type, MemoryType):
            raise DomainValidationError("memory_type must be controlled.")
        if self.source not in {
            MemorySource.PROFILE_AGENT_CANDIDATE,
            MemorySource.BEHAVIOR_CANDIDATE,
        }:
            raise DomainValidationError("candidate source must be an inference source.")
        _require_bounded_text(self.proposed_key, "proposed_key", 80)
        _require_bounded_text(self.proposed_value, "proposed_value", 120)
        _require_bounded_text(self.source_reference, "source_reference", 160)
        _require_bounded_text(self.evidence_summary, "evidence_summary", 240)
        if self.confidence is not None and not Decimal(
            "0"
        ) <= self.confidence <= Decimal("1"):
            raise DomainValidationError("confidence must be between 0 and 1.")
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise DomainValidationError("candidate expires_at must be in the future.")
        if self.reviewed_at is not None:
            require_utc_datetime(self.reviewed_at, "reviewed_at")
        if (
            self.status is MemoryCandidateStatus.PENDING_REVIEW
            and self.reviewed_at is not None
        ):
            raise DomainValidationError("pending Candidate cannot contain reviewed_at.")
        if (
            self.status
            in {MemoryCandidateStatus.ACCEPTED, MemoryCandidateStatus.REJECTED}
            and self.reviewed_at is None
        ):
            raise DomainValidationError("reviewed Candidate requires reviewed_at.")
        require_version(self.version)

    def expire(self) -> MemoryCandidate:
        if self.status is not MemoryCandidateStatus.PENDING_REVIEW:
            return self
        return replace(
            self, status=MemoryCandidateStatus.EXPIRED, version=self.version + 1
        )

    def accept(self, now: datetime) -> MemoryCandidate:
        if self.status is not MemoryCandidateStatus.PENDING_REVIEW:
            raise DomainValidationError("only pending Candidate can be accepted.")
        return replace(
            self,
            status=MemoryCandidateStatus.ACCEPTED,
            reviewed_at=now,
            version=self.version + 1,
        )

    def reject(self, now: datetime) -> MemoryCandidate:
        if self.status is not MemoryCandidateStatus.PENDING_REVIEW:
            raise DomainValidationError("only pending Candidate can be rejected.")
        return replace(
            self,
            status=MemoryCandidateStatus.REJECTED,
            reviewed_at=now,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryRecord:
    memory: UserMemory
    evidence: tuple[MemoryEvidence, ...]

    def __post_init__(self) -> None:
        if self.memory.status is MemoryStatus.ACTIVE and not self.evidence:
            raise DomainValidationError("ACTIVE Memory requires Evidence.")
        if any(item.memory_id != self.memory.id for item in self.evidence):
            raise DomainValidationError("Evidence must reference its Memory.")


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryWriteOutcome:
    record: MemoryRecord
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryReplaceOutcome:
    previous: MemoryRecord
    replacement: MemoryRecord
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateWriteOutcome:
    candidate: MemoryCandidate
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateReviewOutcome:
    candidate: MemoryCandidate
    memory: MemoryRecord | None
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryQueryResult:
    memories: tuple[UserMemory, ...]
    expired_filtered: int
    deleted_filtered: int
    pending_filtered: int
    filtered_reasons: Mapping[UUID, str]
