"""Memory domain invariants and in-memory atomicity."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain.common import DomainValidationError
from app.domain.memory.enums import (
    MemoryCandidateStatus,
    MemoryEvidenceType,
    MemorySource,
    MemoryStatus,
    MemoryType,
)
from app.domain.memory.errors import MemoryConflictError, MemoryVersionConflictError
from app.domain.memory.models import (
    MemoryCandidate,
    MemoryEvidence,
    MemoryRecord,
    UserMemory,
)
from app.memory.service import CreateMemoryCommand, ReplaceMemoryCommand
from tests.phase4a_helpers import future, memory_container, other_user_id

pytestmark = pytest.mark.phase_4a


def _memory(*, status: MemoryStatus = MemoryStatus.ACTIVE) -> UserMemory:
    now = datetime.now(UTC)
    return UserMemory(
        id=uuid4(),
        user_id=uuid4(),
        memory_type=MemoryType.PREFERRED_LOCATION,
        key="preferred_location",
        normalized_value="home",
        display_value="Home",
        status=status,
        source=MemorySource.USER_EXPLICIT,
        confidence=None,
        valid_from=now,
        valid_until=now + timedelta(days=1),
        confirmed_at=(now if status is MemoryStatus.ACTIVE else None),
        created_at=now,
        updated_at=now,
        deleted_at=None,
        version=1,
    )


def test_active_memory_requires_confirmation_evidence_and_valid_time() -> None:
    memory = _memory()
    with pytest.raises(DomainValidationError):
        replace(memory, confirmed_at=None)
    with pytest.raises(DomainValidationError):
        replace(memory, valid_until=memory.valid_from)
    with pytest.raises(DomainValidationError):
        MemoryRecord(memory=memory, evidence=())
    assert "MEDICAL_CONDITION" not in MemoryType.__members__


def test_terminal_memory_transitions_preserve_audit_state() -> None:
    memory = _memory()
    now = datetime.now(UTC)
    assert memory.delete(now).status is MemoryStatus.DELETED
    assert memory.supersede(now).status is MemoryStatus.SUPERSEDED
    assert memory.expire(now).status is MemoryStatus.EXPIRED
    with pytest.raises(DomainValidationError):
        replace(memory, version=0)


def test_evidence_and_candidate_invariants() -> None:
    memory = _memory()
    now = datetime.now(UTC)
    evidence = MemoryEvidence(
        id=uuid4(),
        memory_id=memory.id,
        evidence_type=MemoryEvidenceType.USER_CONFIRMATION,
        source_reference="request:1",
        evidence_summary="USER_CONFIRMATION confirmed a structured preference.",
        source_occurred_at=now,
        created_at=now,
        content_fingerprint="a" * 64,
    )
    assert MemoryRecord(memory=memory, evidence=(evidence,)).evidence == (evidence,)
    candidate = MemoryCandidate(
        id=uuid4(),
        user_id=memory.user_id,
        memory_type=MemoryType.PREFERRED_TIME_OF_DAY,
        proposed_key="preferred_time_of_day",
        proposed_value="morning",
        source=MemorySource.PROFILE_AGENT_CANDIDATE,
        source_reference="draft:1",
        evidence_summary="Structured Draft proposal.",
        confidence=Decimal("0.8"),
        status=MemoryCandidateStatus.PENDING_REVIEW,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        reviewed_at=None,
        version=1,
    )
    assert candidate.accept(now).status is MemoryCandidateStatus.ACCEPTED
    assert candidate.reject(now).status is MemoryCandidateStatus.REJECTED


@pytest.mark.asyncio
async def test_repository_isolation_copy_and_concurrent_idempotency() -> None:
    container = memory_container()
    service = container.memory_application_service
    command = CreateMemoryCommand(
        client_request_id="same-create",
        memory_type=MemoryType.PREFERRED_LOCATION,
        key="preferred_location",
        value="HOME",
        valid_until=None,
    )
    first, second = await asyncio.gather(
        service.create_memory(container.development_user, command),
        service.create_memory(container.development_user, command),
    )
    assert sum(item.created for item in (first, second)) == 1
    assert first.record.memory.id == second.record.memory.id
    assert (
        await container.memory_repository.get_memory(
            other_user_id(), first.record.memory.id, datetime.now(UTC)
        )
        is None
    )
    detached = await service.get_memory(
        container.development_user, first.record.memory.id
    )
    object.__setattr__(detached.memory, "display_value", "polluted")
    reread = await service.get_memory(
        container.development_user, first.record.memory.id
    )
    assert reread.memory.display_value == "HOME"


@pytest.mark.asyncio
async def test_atomic_replace_and_version_conflict() -> None:
    container = memory_container()
    service = container.memory_application_service
    created = await service.create_memory(
        container.development_user,
        CreateMemoryCommand(
            client_request_id="replace-base",
            memory_type=MemoryType.PREFERRED_TIME_OF_DAY,
            key="preferred_time_of_day",
            value="morning",
            valid_until=None,
        ),
    )
    with pytest.raises(MemoryConflictError):
        await service.create_memory(
            container.development_user,
            CreateMemoryCommand(
                client_request_id="conflict-create",
                memory_type=MemoryType.PREFERRED_TIME_OF_DAY,
                key="preferred_time_of_day",
                value="evening",
                valid_until=None,
            ),
        )
    with pytest.raises(MemoryVersionConflictError):
        await service.replace_memory(
            container.development_user,
            created.record.memory.id,
            ReplaceMemoryCommand(
                client_request_id="bad-replace",
                expected_version=99,
                value="evening",
                valid_until=future(),
            ),
        )
    still_active = await service.get_memory(
        container.development_user, created.record.memory.id
    )
    assert still_active.memory.status is MemoryStatus.ACTIVE
    replaced = await service.replace_memory(
        container.development_user,
        created.record.memory.id,
        ReplaceMemoryCommand(
            client_request_id="good-replace",
            expected_version=1,
            value="evening",
            valid_until=future(),
        ),
    )
    assert replaced.previous.memory.status is MemoryStatus.SUPERSEDED
    assert replaced.replacement.memory.status is MemoryStatus.ACTIVE
    active = await container.memory_repository.list_active_for_context(
        container.development_user.id, datetime.now(UTC)
    )
    assert [item.id for item in active.memories] == [replaced.replacement.memory.id]
