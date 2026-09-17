"""Real MySQL immutability and user-isolation contract for Context snapshots."""

import asyncio
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.context.enums import AgentType, ContextDegradedMode, ContextSectionName
from app.domain.context.models import (
    BuiltContext,
    ContextItem,
    ContextSection,
    ContextSnapshotReference,
    FrozenContextSnapshot,
)
from app.domain.memory.errors import ContextSnapshotVersionMismatchError
from app.persistence.database import Database
from app.persistence.mysql.context_snapshot_repository import (
    MySQLContextSnapshotRepository,
)
from app.persistence.mysql.models import ContextSnapshotModel, UserAccountModel
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.factories import TEST_NOW, make_user


def _snapshot(user_id):
    reference = ContextSnapshotReference(
        id=uuid4(),
        user_id=user_id,
        agent_type=AgentType.PLAN_GENERATION,
        contract_version="plan-context-v1",
        policy_version="context-policy-v1",
        context_fingerprint="a" * 64,
        context_audit_id=uuid4(),
        profile_id=None,
        profile_version=None,
        constraint_versions=(),
        memory_versions=(),
        degraded_mode=ContextDegradedMode.NONE,
        created_at=TEST_NOW,
    )
    context = BuiltContext(
        id=uuid4(),
        user_id=user_id,
        agent_type=AgentType.PLAN_GENERATION,
        contract_version="plan-context-v1",
        sections=(
            ContextSection(
                name=ContextSectionName.CURRENT_TASK,
                source="request",
                generated_at=TEST_NOW,
                version="v1",
                items=(ContextItem(key="goal", value="fitness", source="request"),),
            ),
        ),
        conflicts=(),
        degraded_mode=ContextDegradedMode.NONE,
        character_count=14,
        audit_id=reference.context_audit_id,
        created_at=TEST_NOW,
    )
    return FrozenContextSnapshot(reference=reference, context=context)


class _FirstTwoAbsentReads:
    def __init__(self) -> None:
        self.count = 0
        self._lock = asyncio.Lock()
        self._release = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self.count += 1
            should_wait = self.count <= 2
            if self.count == 2:
                self._release.set()
        if should_wait:
            await self._release.wait()


class _GatedContextSession(AsyncSession):
    async def scalar(
        self,
        statement: Any,
        params: Any = None,
        **kwargs: Any,
    ) -> Any:
        result = await super().scalar(statement, params=params, **kwargs)
        gate = self.info.get("context_scope_gate")
        if result is None and isinstance(gate, _FirstTwoAbsentReads):
            await gate.wait()
        return result


def _gated_repository(
    database: Database,
) -> tuple[MySQLContextSnapshotRepository, _FirstTwoAbsentReads]:
    gate = _FirstTwoAbsentReads()
    sessions = async_sessionmaker(
        bind=database.engine,
        class_=_GatedContextSession,
        expire_on_commit=False,
        info={"context_scope_gate": gate},
    )
    return MySQLContextSnapshotRepository(sessions), gate


async def _scope_row_count(
    database: Database,
    *,
    user_id: UUID,
    scope_id: str,
) -> int:
    async with database.session_factory() as session:
        count = await session.scalar(
            select(func.count(ContextSnapshotModel.id)).where(
                ContextSnapshotModel.user_id == str(user_id),
                ContextSnapshotModel.agent_type == AgentType.PLAN_GENERATION.value,
                ContextSnapshotModel.scope_id == scope_id,
            )
        )
    assert count is not None
    return count


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_context_snapshot_is_immutable_scoped_and_restart_readable(
    mysql_test_database: Database,
) -> None:
    user = replace(make_user(), email=f"context-{uuid4().hex}@fitweek.test")
    other_user = replace(make_user(), email=f"context-{uuid4().hex}@fitweek.test")
    repository = MySQLContextSnapshotRepository(mysql_test_database.session_factory)
    snapshot = _snapshot(user.id)
    try:
        users = MySQLUserAccountRepository(mysql_test_database.session_factory)
        await users.save(user)
        await users.save(other_user)
        assert await repository.save(scope_id="run:one", snapshot=snapshot) == snapshot
        restored = await MySQLContextSnapshotRepository(
            mysql_test_database.session_factory
        ).get(user.id, snapshot.reference.id)
        assert restored == snapshot
        assert (
            await repository.get_by_scope(user.id, AgentType.PLAN_GENERATION, "run:one")
            == snapshot
        )
        assert await repository.get(other_user.id, snapshot.reference.id) is None
        with pytest.raises(ContextSnapshotVersionMismatchError):
            await repository.save(
                scope_id="run:one",
                snapshot=replace(
                    snapshot,
                    reference=replace(snapshot.reference, context_fingerprint="b" * 64),
                ),
            )
    finally:
        async with mysql_test_database.session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(ContextSnapshotModel).where(
                        ContextSnapshotModel.user_id.in_(
                            (str(user.id), str(other_user.id))
                        )
                    )
                )
                await session.execute(
                    delete(UserAccountModel).where(
                        UserAccountModel.id.in_((str(user.id), str(other_user.id)))
                    )
                )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_context_snapshot_concurrent_scope_recovers_committed_winner(
    mysql_test_database: Database,
) -> None:
    user = replace(make_user(), email=f"context-race-{uuid4().hex}@fitweek.test")
    repository = MySQLContextSnapshotRepository(mysql_test_database.session_factory)
    same_scope = f"same:{uuid4().hex}"
    different_scope = f"different:{uuid4().hex}"
    collision_scope = f"collision:{uuid4().hex}"
    first = _snapshot(user.id)
    second = _snapshot(user.id)
    assert first.reference.id != second.reference.id
    assert first.reference.context_audit_id != second.reference.context_audit_id
    assert first.reference.context_fingerprint == second.reference.context_fingerprint
    try:
        await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)

        same_repository, same_gate = _gated_repository(mysql_test_database)
        same_results = await asyncio.gather(
            same_repository.save(scope_id=same_scope, snapshot=first),
            same_repository.save(scope_id=same_scope, snapshot=second),
        )
        assert same_gate.count == 2
        assert same_results[0] == same_results[1]
        same_winner = await repository.get_by_scope(
            user.id,
            AgentType.PLAN_GENERATION,
            same_scope,
        )
        assert same_winner == same_results[0]
        assert same_winner in (first, second)
        assert (
            await _scope_row_count(
                mysql_test_database,
                user_id=user.id,
                scope_id=same_scope,
            )
            == 1
        )
        assert (
            await repository.save(scope_id=same_scope, snapshot=same_winner)
            == same_winner
        )

        different_first = _snapshot(user.id)
        different_second = _snapshot(user.id)
        different_second = replace(
            different_second,
            reference=replace(
                different_second.reference,
                context_fingerprint="b" * 64,
            ),
        )
        different_repository, different_gate = _gated_repository(mysql_test_database)
        different_results = await asyncio.gather(
            different_repository.save(
                scope_id=different_scope,
                snapshot=different_first,
            ),
            different_repository.save(
                scope_id=different_scope,
                snapshot=different_second,
            ),
            return_exceptions=True,
        )
        assert different_gate.count == 2
        different_winners = [
            result
            for result in different_results
            if isinstance(result, FrozenContextSnapshot)
        ]
        different_conflicts = [
            result
            for result in different_results
            if isinstance(result, ContextSnapshotVersionMismatchError)
        ]
        assert len(different_winners) == 1
        assert len(different_conflicts) == 1
        assert isinstance(different_conflicts[0].__cause__, IntegrityError)
        different_winner = different_winners[0]
        assert (
            await repository.get_by_scope(
                user.id,
                AgentType.PLAN_GENERATION,
                different_scope,
            )
            == different_winner
        )
        assert (
            await _scope_row_count(
                mysql_test_database,
                user_id=user.id,
                scope_id=different_scope,
            )
            == 1
        )

        collision_candidate = _snapshot(user.id)
        collision_candidate = replace(
            collision_candidate,
            reference=replace(
                collision_candidate.reference,
                id=same_winner.reference.id,
            ),
        )
        captured_errors: list[IntegrityError] = []

        def capture_integrity(exception_context: Any) -> None:
            error = exception_context.sqlalchemy_exception
            if isinstance(error, IntegrityError):
                captured_errors.append(error)

        event.listen(
            mysql_test_database.engine.sync_engine,
            "handle_error",
            capture_integrity,
        )
        try:
            with pytest.raises(IntegrityError) as unrelated:
                await repository.save(
                    scope_id=collision_scope,
                    snapshot=collision_candidate,
                )
        finally:
            event.remove(
                mysql_test_database.engine.sync_engine,
                "handle_error",
                capture_integrity,
            )
        assert captured_errors
        assert unrelated.value is captured_errors[-1]
        assert (
            await repository.get_by_scope(
                user.id,
                AgentType.PLAN_GENERATION,
                collision_scope,
            )
            is None
        )
        assert (
            await _scope_row_count(
                mysql_test_database,
                user_id=user.id,
                scope_id=collision_scope,
            )
            == 0
        )
        assert (
            await repository.get_by_scope(
                user.id,
                AgentType.PLAN_GENERATION,
                same_scope,
            )
            == same_winner
        )
    finally:
        async with mysql_test_database.session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(ContextSnapshotModel).where(
                        ContextSnapshotModel.user_id == str(user.id)
                    )
                )
                await session.execute(
                    delete(UserAccountModel).where(UserAccountModel.id == str(user.id))
                )
