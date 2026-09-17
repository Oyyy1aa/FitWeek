"""MySQL MemoryRepository contract: durable explicit Memory and active query."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.domain.memory.enums import (
    MemoryCandidateStatus,
    MemoryEvidenceType,
    MemorySource,
    MemoryStatus,
    MemoryType,
)
from app.domain.memory.models import MemoryCandidate, MemoryEvidence, UserMemory
from app.persistence.database import Database
from app.persistence.mysql.models import (
    IdempotencyRecordModel,
    MemoryCandidateModel,
    MemoryEvidenceModel,
    MemoryItemModel,
    UserAccountModel,
)
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.factories import TEST_NOW, make_user


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_memory_repository_persists_active_memory_across_instances(
    mysql_test_database: Database,
) -> None:
    from app.persistence.mysql.memory_repository import MySQLMemoryRepository

    user = replace(make_user(), email=f"memory-{uuid4().hex}@fitweek.test")
    memory = UserMemory(
        id=uuid4(),
        user_id=user.id,
        memory_type=MemoryType.PREFERRED_LOCATION,
        key="preferred_location",
        normalized_value="home",
        display_value="Home",
        status=MemoryStatus.ACTIVE,
        source=MemorySource.USER_EXPLICIT,
        confidence=None,
        valid_from=TEST_NOW,
        valid_until=None,
        confirmed_at=TEST_NOW,
        created_at=TEST_NOW,
        updated_at=TEST_NOW,
        deleted_at=None,
        version=1,
    )
    evidence = MemoryEvidence(
        id=uuid4(),
        memory_id=memory.id,
        evidence_type=MemoryEvidenceType.USER_CONFIRMATION,
        source_reference="test:memory",
        evidence_summary="Structured test evidence.",
        source_occurred_at=TEST_NOW,
        created_at=TEST_NOW,
        content_fingerprint="a" * 64,
    )
    try:
        await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
        repository = MySQLMemoryRepository(mysql_test_database.session_factory)
        created = await repository.create_memory(
            client_request_id="memory-create",
            payload_fingerprint="b" * 64,
            memory=memory,
            evidence=evidence,
        )
        assert created.created is True
        restored = await MySQLMemoryRepository(
            mysql_test_database.session_factory
        ).get_memory(user.id, memory.id, TEST_NOW)
        assert restored is not None and restored.memory == memory
        assert (
            await repository.list_active_for_context(user.id, TEST_NOW)
        ).memories == (memory,)
    finally:
        async with mysql_test_database.session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(MemoryEvidenceModel).where(
                        MemoryEvidenceModel.memory_id == str(memory.id)
                    )
                )
                await session.execute(
                    delete(MemoryItemModel).where(MemoryItemModel.id == str(memory.id))
                )
                await session.execute(
                    delete(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.user_id == str(user.id)
                    )
                )
                await session.execute(
                    delete(UserAccountModel).where(UserAccountModel.id == str(user.id))
                )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_memory_candidate_idempotent_accept_is_durable(
    mysql_test_database: Database,
) -> None:
    from app.persistence.mysql.memory_repository import MySQLMemoryRepository

    user = replace(make_user(), email=f"memory-{uuid4().hex}@fitweek.test")
    candidate = MemoryCandidate(
        id=uuid4(),
        user_id=user.id,
        memory_type=MemoryType.PREFERRED_LOCATION,
        proposed_key="preferred_location",
        proposed_value="home",
        source=MemorySource.PROFILE_AGENT_CANDIDATE,
        source_reference="profile-draft:test",
        evidence_summary="Structured candidate evidence.",
        confidence=None,
        status=MemoryCandidateStatus.PENDING_REVIEW,
        created_at=TEST_NOW,
        expires_at=TEST_NOW.replace(year=TEST_NOW.year + 1),
        reviewed_at=None,
        version=1,
    )
    memory = UserMemory(
        id=uuid4(),
        user_id=user.id,
        memory_type=candidate.memory_type,
        key=candidate.proposed_key,
        normalized_value=candidate.proposed_value,
        display_value="Home",
        status=MemoryStatus.ACTIVE,
        source=MemorySource.USER_EXPLICIT,
        confidence=None,
        valid_from=TEST_NOW,
        valid_until=None,
        confirmed_at=TEST_NOW,
        created_at=TEST_NOW,
        updated_at=TEST_NOW,
        deleted_at=None,
        version=1,
    )
    evidence = MemoryEvidence(
        id=uuid4(),
        memory_id=memory.id,
        evidence_type=MemoryEvidenceType.USER_CONFIRMATION,
        source_reference="candidate:test",
        evidence_summary="User confirmed the candidate.",
        source_occurred_at=TEST_NOW,
        created_at=TEST_NOW,
        content_fingerprint="c" * 64,
    )
    try:
        await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
        repository = MySQLMemoryRepository(mysql_test_database.session_factory)
        first = await repository.create_candidate(
            client_request_id="candidate-create",
            payload_fingerprint="d" * 64,
            candidate=candidate,
        )
        second = await repository.create_candidate(
            client_request_id="candidate-create",
            payload_fingerprint="d" * 64,
            candidate=candidate,
        )
        assert first.created is True
        assert second == replace(first, created=False)
        accepted = await repository.accept_candidate(
            user_id=user.id,
            candidate_id=candidate.id,
            expected_version=1,
            client_request_id="candidate-accept",
            payload_fingerprint="e" * 64,
            confirmed_value="home",
            memory=memory,
            evidence=evidence,
            now=TEST_NOW,
        )
        assert accepted.candidate.status is MemoryCandidateStatus.ACCEPTED
        assert accepted.memory is not None and accepted.memory.memory == memory
        durable = await MySQLMemoryRepository(
            mysql_test_database.session_factory
        ).get_candidate(user.id, candidate.id, TEST_NOW)
        assert durable is not None and durable.status is MemoryCandidateStatus.ACCEPTED
    finally:
        async with mysql_test_database.session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(MemoryEvidenceModel).where(
                        MemoryEvidenceModel.memory_id == str(memory.id)
                    )
                )
                await session.execute(
                    delete(MemoryItemModel).where(MemoryItemModel.id == str(memory.id))
                )
                await session.execute(
                    delete(MemoryCandidateModel).where(
                        MemoryCandidateModel.id == str(candidate.id)
                    )
                )
                await session.execute(
                    delete(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.user_id == str(user.id)
                    )
                )
                await session.execute(
                    delete(UserAccountModel).where(UserAccountModel.id == str(user.id))
                )
