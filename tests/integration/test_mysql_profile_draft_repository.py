"""MySQL contract coverage for durable Profile Agent drafts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select

from app.domain.common import LocationType, RepositoryUniqueError
from app.domain.context.enums import ContextDegradedMode
from app.domain.model_gateway.enums import FallbackType
from app.domain.profile_agent.models import (
    ProfileAgentDraft,
    ProfileAgentOutput,
    ProfileDraftStatus,
    ScopeStatus,
)
from app.domain.profiles.models import FitnessGoal
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AuditEventModel,
    ContextSnapshotModel,
    ProfileDraftModel,
    UserAccountModel,
)
from app.persistence.mysql.profile_draft_repository import (
    MySQLProfileDraftRepository,
)


def _draft(
    *,
    user_id: UUID,
    snapshot_id: UUID,
    client_request_id: str,
    now: datetime,
    draft_id: UUID | None = None,
) -> ProfileAgentDraft:
    return ProfileAgentDraft(
        id=draft_id or uuid4(),
        request_id=uuid4(),
        client_request_id=client_request_id,
        user_id=user_id,
        request_payload_fingerprint="a" * 64,
        input_fingerprint="b" * 64,
        output=ProfileAgentOutput(
            weekly_frequency=3,
            max_session_minutes=35,
            goals=(FitnessGoal.GENERAL_FITNESS,),
            equipment=("resistance_band",),
            locations=(LocationType.HOME,),
            scope_status=ScopeStatus.SUPPORTED,
            explanation_summary="Safe structured draft for persistence.",
        ),
        prompt_version="profile-agent-v1",
        provider_summary="controlled-http",
        fallback_used=True,
        fallback_type=FallbackType.TEMPLATE,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        context_snapshot_reference_id=snapshot_id,
        context_fingerprint="c" * 64,
        context_contract_version="context-v1",
        context_policy_version="policy-v1",
        context_degraded_mode=ContextDegradedMode.NO_MEMORY,
        context_included_memory_count=2,
    )


async def _seed_user_and_snapshot(
    database: Database, user_id: UUID, snapshot_id: UUID, now: datetime
) -> None:
    async with database.session_factory() as session:
        async with session.begin():
            session.add(
                UserAccountModel(
                    id=str(user_id),
                    email=f"profile-draft-repository-{user_id.hex}@fitweek.test",
                    display_name="Profile Draft Repository",
                    timezone="Asia/Shanghai",
                    status="ACTIVE",
                    created_at=now.replace(tzinfo=None),
                    updated_at=now.replace(tzinfo=None),
                    version=1,
                )
            )
            await session.flush()
            session.add(
                ContextSnapshotModel(
                    id=str(snapshot_id),
                    user_id=str(user_id),
                    agent_type="PROFILE_AGENT",
                    scope_id=f"profile-draft-{snapshot_id}",
                    fingerprint="c" * 64,
                    contract_version="context-v1",
                    policy_version="policy-v1",
                    content={"safe_references": []},
                    character_count=0,
                    token_estimate=0,
                    created_at=now.replace(tzinfo=None),
                )
            )


async def _cleanup(database: Database, user_id: UUID) -> None:
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(ProfileDraftModel).where(
                    ProfileDraftModel.user_id == str(user_id)
                )
            )
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id == str(user_id))
            )
            await session.execute(
                delete(ContextSnapshotModel).where(
                    ContextSnapshotModel.user_id == str(user_id)
                )
            )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id == str(user_id))
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_profile_draft_round_trip_idempotency_reject_and_expiry(
    mysql_test_database: Database,
) -> None:
    now = datetime.now(UTC).replace(microsecond=123456)
    user_id, other_user_id = uuid4(), uuid4()
    snapshot_id, other_snapshot_id = uuid4(), uuid4()
    await _seed_user_and_snapshot(mysql_test_database, user_id, snapshot_id, now)
    await _seed_user_and_snapshot(
        mysql_test_database, other_user_id, other_snapshot_id, now
    )
    repository = MySQLProfileDraftRepository(
        mysql_test_database.session_factory, clock=lambda: now
    )
    draft = _draft(
        user_id=user_id,
        snapshot_id=snapshot_id,
        client_request_id=f"round-trip-{uuid4().hex}",
        now=now,
    )
    try:
        await repository.save(draft)
        restored = await MySQLProfileDraftRepository(
            mysql_test_database.session_factory, clock=lambda: now
        ).get(draft.id, user_id)
        assert restored == draft
        assert await repository.get(draft.id, other_user_id) is None
        assert await repository.list(user_id) == [draft]

        concurrent_request_id = f"concurrent-{uuid4().hex}"
        concurrent_a = _draft(
            user_id=user_id,
            snapshot_id=snapshot_id,
            client_request_id=concurrent_request_id,
            now=now,
        )
        concurrent_b = _draft(
            user_id=user_id,
            snapshot_id=snapshot_id,
            client_request_id=concurrent_request_id,
            now=now,
        )
        outcomes = await asyncio.gather(
            repository.save(concurrent_a),
            repository.save(concurrent_b),
            return_exceptions=True,
        )
        assert sum(isinstance(item, ProfileAgentDraft) for item in outcomes) == 1
        assert sum(isinstance(item, RepositoryUniqueError) for item in outcomes) == 1
        async with mysql_test_database.session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ProfileDraftModel)
                .where(
                    ProfileDraftModel.user_id == str(user_id),
                    ProfileDraftModel.client_request_id == concurrent_request_id,
                )
            )
        assert count == 1

        rejected = await repository.reject(
            draft_id=draft.id,
            user_id=user_id,
            client_request_id=f"reject-{uuid4().hex}",
            expected_draft_version=1,
            reject_fingerprint="d" * 64,
            now=now + timedelta(minutes=1),
        )
        assert rejected.created is True
        rejected_draft = await repository.get_draft_for_review(draft.id, user_id)
        assert rejected_draft is not None
        assert rejected_draft.status is ProfileDraftStatus.REJECTED
        assert rejected_draft.version == 2

        expiring = _draft(
            user_id=user_id,
            snapshot_id=snapshot_id,
            client_request_id=f"expires-{uuid4().hex}",
            now=now,
        )
        await repository.save(expiring)
        expired_repository = MySQLProfileDraftRepository(
            mysql_test_database.session_factory,
            clock=lambda: now + timedelta(hours=2),
        )
        assert await expired_repository.get(expiring.id, user_id) is None
        expired = await expired_repository.get_draft_for_review(expiring.id, user_id)
        assert expired is not None
        assert expired.status is ProfileDraftStatus.EXPIRED
        assert expired.version == 2
    finally:
        await _cleanup(mysql_test_database, user_id)
        await _cleanup(mysql_test_database, other_user_id)
