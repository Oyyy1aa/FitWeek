"""Memory Service, Candidate review, Evidence, and filtering behavior."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.memory.enums import (
    MemoryCandidateStatus,
    MemorySource,
    MemoryStatus,
    MemoryType,
)
from app.domain.memory.errors import (
    MemoryCandidateExpiredError,
    MemoryCandidateIdempotencyConflictError,
    MemoryInvalidValueError,
    MemoryVersionConflictError,
)
from app.memory.candidate_service import (
    AcceptCandidateCommand,
    CreateCandidateCommand,
    RejectCandidateCommand,
)
from app.memory.service import CreateMemoryCommand, UpdateMemoryCommand
from tests.phase4a_helpers import future, memory_container

pytestmark = pytest.mark.phase_4a


@pytest.mark.asyncio
async def test_explicit_crud_evidence_update_delete_and_duplicate() -> None:
    container = memory_container()
    service = container.memory_application_service
    command = CreateMemoryCommand(
        client_request_id="explicit-1",
        memory_type=MemoryType.PREFERRED_EQUIPMENT,
        key="preferred_equipment",
        value="Resistance Band",
        valid_until=future(),
    )
    first = await service.create_memory(container.development_user, command)
    duplicate = await service.create_memory(container.development_user, command)
    assert first.created and not duplicate.created
    assert first.record.memory.status is MemoryStatus.ACTIVE
    assert len(first.record.evidence) == 1
    updated = await service.update_memory(
        container.development_user,
        first.record.memory.id,
        UpdateMemoryCommand(
            expected_version=1,
            value="Dumbbell",
            valid_until=future(48),
        ),
    )
    assert updated.memory.version == 2
    assert len(updated.evidence) == 2
    with pytest.raises(MemoryVersionConflictError):
        await service.update_memory(
            container.development_user,
            first.record.memory.id,
            UpdateMemoryCommand(
                expected_version=1,
                value="Mat",
                valid_until=None,
            ),
        )
    deleted = await service.delete_memory(
        container.development_user, first.record.memory.id, 2
    )
    assert deleted.memory.status is MemoryStatus.DELETED
    replay = await service.delete_memory(
        container.development_user, first.record.memory.id, 2
    )
    assert replay.memory == deleted.memory


@pytest.mark.asyncio
async def test_medical_values_are_rejected() -> None:
    container = memory_container()
    with pytest.raises(MemoryInvalidValueError):
        await container.memory_application_service.create_memory(
            container.development_user,
            CreateMemoryCommand(
                client_request_id="medical",
                memory_type=MemoryType.TRAINING_STYLE_PREFERENCE,
                key="training_style",
                value="injury treatment plan",
                valid_until=None,
            ),
        )


def _candidate_command(request_id: str = "candidate-1") -> CreateCandidateCommand:
    return CreateCandidateCommand(
        client_request_id=request_id,
        memory_type=MemoryType.PREFERRED_TIME_OF_DAY,
        key="preferred_time_of_day",
        value="Morning",
        source=MemorySource.PROFILE_AGENT_CANDIDATE,
        source_reference="draft:controlled-1",
        evidence_summary="Profile Draft proposed a structured time preference.",
        confidence=Decimal("0.75"),
        expires_at=future(),
    )


@pytest.mark.asyncio
async def test_candidate_accept_requires_review_and_creates_evidence() -> None:
    container = memory_container()
    service = container.memory_application_service
    created = await service.create_candidate(
        container.development_user, _candidate_command()
    )
    before = await container.memory_repository.list_active_for_context(
        container.development_user.id, datetime.now(UTC)
    )
    assert before.memories == ()
    accepted = await service.accept_candidate(
        container.development_user,
        created.candidate.id,
        AcceptCandidateCommand(
            client_request_id="accept-1",
            expected_candidate_version=1,
            confirmed_value=" morning ",
            valid_until=future(),
        ),
    )
    assert accepted.candidate.status is MemoryCandidateStatus.ACCEPTED
    assert accepted.memory is not None
    assert accepted.memory.memory.status is MemoryStatus.ACTIVE
    assert accepted.memory.evidence[0].source_reference == "draft:controlled-1"
    replay = await service.accept_candidate(
        container.development_user,
        created.candidate.id,
        AcceptCandidateCommand(
            client_request_id="accept-1",
            expected_candidate_version=1,
            confirmed_value="morning",
            valid_until=accepted.memory.memory.valid_until,
        ),
    )
    assert not replay.created


@pytest.mark.asyncio
async def test_candidate_reject_and_idempotency_conflict() -> None:
    container = memory_container()
    service = container.memory_application_service
    candidate = await service.create_candidate(
        container.development_user, _candidate_command("candidate-reject")
    )
    rejected = await service.reject_candidate(
        container.development_user,
        candidate.candidate.id,
        RejectCandidateCommand(
            client_request_id="reject-1", expected_candidate_version=1
        ),
    )
    assert rejected.candidate.status is MemoryCandidateStatus.REJECTED
    assert rejected.memory is None
    with pytest.raises(MemoryCandidateIdempotencyConflictError):
        await service.reject_candidate(
            container.development_user,
            candidate.candidate.id,
            RejectCandidateCommand(
                client_request_id="reject-1", expected_candidate_version=2
            ),
        )


@pytest.mark.asyncio
async def test_candidate_ttl_is_derived_and_cannot_be_accepted() -> None:
    container = memory_container()
    service = container.memory_application_service
    candidate = await service.create_candidate(
        container.development_user, _candidate_command("candidate-expire")
    )
    await container.memory_repository.get_candidate(
        container.development_user.id,
        candidate.candidate.id,
        candidate.candidate.expires_at + timedelta(seconds=1),
    )
    with pytest.raises(MemoryCandidateExpiredError):
        await service.accept_candidate(
            container.development_user,
            candidate.candidate.id,
            AcceptCandidateCommand(
                client_request_id="accept-expired",
                expected_candidate_version=1,
                confirmed_value="morning",
                valid_until=None,
            ),
        )
