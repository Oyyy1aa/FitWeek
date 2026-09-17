"""MySQL persistence contracts for Calendar operation control facts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.calendar_operations import (
    CalendarOperationIdempotencyConflict,
    CalendarOperationService,
    CreateCalendarOperationCommand,
)
from app.calendar_operations.gateway import CalendarWriteGateway
from app.calendar_operations.payload_policy import CalendarPayloadPolicy
from app.domain.calendar_operations.enums import (
    CalendarAttemptOutcome,
    CalendarBindingStatus,
    CalendarOperationDraftStatus,
    CalendarOperationItemStatus,
    CalendarOperationType,
)
from app.domain.calendar_operations.models import (
    CalendarEventBinding,
    CalendarEventPayload,
    CalendarOperationAttempt,
    CalendarOperationDraft,
    CalendarOperationItem,
)
from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.sessions.models import WorkoutSessionStatus
from app.orchestration.clock import FakeClock
from app.persistence.database import Database
from app.persistence.mysql.calendar_operation_repository import (
    MySQLCalendarOperationRepository,
)
from app.persistence.mysql.models import (
    Base,
    CalendarEventBindingModel,
    CalendarOperationAttemptModel,
    CalendarOperationDraftModel,
    CalendarOperationItemModel,
    SessionExerciseModel,
    UserAccountModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)
from app.persistence.mysql.plan_repository import MySQLPlanRepository
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.factories import TEST_NOW, make_plan, make_user


def _run_alembic(
    database_url: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def _run_offline(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments, "--sql"],
        check=False,
        capture_output=True,
        text=True,
    )


def _payload(session_id: UUID, marker: str) -> CalendarEventPayload:
    start = datetime(2026, 8, 1, 9, 30, 0, 123456, tzinfo=UTC)
    return CalendarEventPayload(
        session_id=session_id,
        stable_uid=f"uid-{marker}",
        summary=f"summary-{marker}",
        description=f"description-{marker}",
        start=start,
        end=start + timedelta(minutes=45),
        timezone="UTC",
        transparency="OPAQUE",
        payload_fingerprint=(marker * 64)[:64],
    )


def _binding(
    user_id: UUID,
    root_plan_id: UUID,
    session_id: UUID,
    marker: str,
) -> CalendarEventBinding:
    return CalendarEventBinding(
        id=uuid4(),
        user_id=user_id,
        provider="test-calendar",
        calendar_id="calendar-0017",
        root_plan_id=root_plan_id,
        session_id=session_id,
        external_event_id=f"event-{marker}",
        stable_uid=f"uid-{marker}",
        last_payload_fingerprint=(marker * 64)[:64],
        status=CalendarBindingStatus.ACTIVE,
        created_at=TEST_NOW,
        updated_at=TEST_NOW,
        version=1,
    )


def _draft(
    user_id: UUID,
    root_plan_id: UUID,
    *,
    binding_id: UUID | None = None,
    request_id: str | None = None,
) -> CalendarOperationDraft:
    first_session, second_session = uuid4(), uuid4()
    first = CalendarOperationItem(
        id=uuid4(),
        operation_type=CalendarOperationType.CREATE,
        session_id=first_session,
        operation_key=hashlib.sha256(f"calendar-create-{uuid4()}".encode()).hexdigest(),
        payload=_payload(first_session, "a"),
        binding_id=None,
        status=CalendarOperationItemStatus.PENDING,
    )
    second = CalendarOperationItem(
        id=uuid4(),
        operation_type=(
            CalendarOperationType.DELETE
            if binding_id is not None
            else CalendarOperationType.CREATE
        ),
        session_id=second_session,
        operation_key=hashlib.sha256(f"calendar-second-{uuid4()}".encode()).hexdigest(),
        payload=None if binding_id is not None else _payload(second_session, "b"),
        binding_id=binding_id,
        status=CalendarOperationItemStatus.PENDING,
    )
    return CalendarOperationDraft(
        id=uuid4(),
        user_id=user_id,
        client_request_id=request_id or f"calendar-request-{uuid4().hex}",
        request_fingerprint="f" * 64,
        provider="test-calendar",
        calendar_id="calendar-0017",
        root_plan_id=root_plan_id,
        revision=1,
        plan_version=1,
        items=(first, second),
        status=CalendarOperationDraftStatus.PENDING_REVIEW,
        created_at=TEST_NOW,
        updated_at=TEST_NOW,
    )


async def _cleanup(
    database: Database,
    user_ids: tuple[UUID, ...],
    plan_ids: tuple[UUID, ...] = (),
) -> None:
    if not user_ids:
        return
    values = [str(value) for value in user_ids]
    plans = [str(value) for value in plan_ids]
    async with database.session_factory() as session:
        async with session.begin():
            draft_ids = list(
                (
                    await session.scalars(
                        select(CalendarOperationDraftModel.id).where(
                            CalendarOperationDraftModel.user_id.in_(values)
                        )
                    )
                ).all()
            )
            item_ids = list(
                (
                    await session.scalars(
                        select(CalendarOperationItemModel.id).where(
                            CalendarOperationItemModel.draft_id.in_(draft_ids)
                        )
                    )
                ).all()
            )
            if draft_ids:
                await session.execute(
                    delete(CalendarOperationAttemptModel).where(
                        CalendarOperationAttemptModel.draft_id.in_(draft_ids)
                    )
                )
            if item_ids:
                await session.execute(
                    delete(CalendarOperationItemModel).where(
                        CalendarOperationItemModel.id.in_(item_ids)
                    )
                )
            if draft_ids:
                await session.execute(
                    delete(CalendarOperationDraftModel).where(
                        CalendarOperationDraftModel.id.in_(draft_ids)
                    )
                )
            await session.execute(
                delete(CalendarEventBindingModel).where(
                    CalendarEventBindingModel.user_id.in_(values)
                )
            )
            if plans:
                session_ids = select(WorkoutSessionModel.id).where(
                    WorkoutSessionModel.plan_id.in_(plans)
                )
                await session.execute(
                    delete(SessionExerciseModel).where(
                        SessionExerciseModel.session_id.in_(session_ids)
                    )
                )
                await session.execute(
                    delete(WorkoutSessionModel).where(
                        WorkoutSessionModel.plan_id.in_(plans)
                    )
                )
                await session.execute(
                    delete(WeeklyPlanModel).where(WeeklyPlanModel.id.in_(plans))
                )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id.in_(values))
            )


def test_calendar_operation_migration_offline_online_and_schema_contract(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    """The 0010 migration keeps all four normalized facts contractually exact."""

    expected_columns = {
        "calendar_event_binding": {
            "id",
            "user_id",
            "provider",
            "calendar_id",
            "root_plan_id",
            "session_id",
            "external_event_id",
            "stable_uid",
            "last_payload_fingerprint",
            "status",
            "created_at",
            "updated_at",
            "version",
        },
        "calendar_operation_draft": {
            "id",
            "user_id",
            "client_request_id",
            "request_fingerprint",
            "provider",
            "calendar_id",
            "root_plan_id",
            "revision",
            "plan_version",
            "status",
            "created_at",
            "updated_at",
            "approved_at",
            "rejected_at",
            "version",
        },
        "calendar_operation_item": {
            "id",
            "draft_id",
            "sequence_no",
            "operation_type",
            "session_id",
            "operation_key",
            "binding_id",
            "stable_uid",
            "summary",
            "description",
            "start_at",
            "end_at",
            "timezone",
            "transparency",
            "payload_fingerprint",
            "status",
            "attempt_count",
            "last_error_code",
        },
        "calendar_operation_attempt": {
            "id",
            "user_id",
            "draft_id",
            "item_id",
            "attempt_no",
            "outcome",
            "error_code",
            "response_reference_hash",
            "started_at",
            "finished_at",
        },
    }
    assert set(expected_columns) <= set(Base.metadata.tables)
    for name, columns in expected_columns.items():
        assert set(Base.metadata.tables[name].columns.keys()) == columns
    assert {
        constraint.name
        for constraint in Base.metadata.tables["calendar_event_binding"].constraints
        if constraint.name is not None
    } >= {"uq_calendar_binding_logical", "uq_calendar_binding_external"}
    assert {
        constraint.name
        for constraint in Base.metadata.tables["calendar_operation_draft"].constraints
        if constraint.name is not None
    } >= {"uq_calendar_draft_request"}
    assert {
        constraint.name
        for constraint in Base.metadata.tables["calendar_operation_item"].constraints
        if constraint.name is not None
    } >= {"uq_calendar_item_sequence", "uq_calendar_item_key"}
    assert {
        constraint.name
        for constraint in Base.metadata.tables["calendar_operation_attempt"].constraints
        if constraint.name is not None
    } >= {"uq_calendar_attempt_number"}
    assert {
        index.name for index in Base.metadata.tables["calendar_operation_draft"].indexes
    } == {"ix_calendar_draft_user_plan_revision", "ix_calendar_draft_user_status"}
    assert (
        Base.metadata.tables["calendar_operation_item"].c.operation_key.type.length
        == 64
    )

    offline_upgrade = _run_offline(
        "upgrade", "0009_ics_export_persistence:0010_calendar_operation_persistence"
    )
    assert offline_upgrade.returncode == 0, offline_upgrade.stderr
    assert "CREATE TABLE calendar_operation_draft" in offline_upgrade.stdout
    assert "DATETIME(6)" in offline_upgrade.stdout
    assert "operation_key VARCHAR(64) NOT NULL" in offline_upgrade.stdout
    offline_downgrade = _run_offline(
        "downgrade", "0010_calendar_operation_persistence:0009_ics_export_persistence"
    )
    assert offline_downgrade.returncode == 0, offline_downgrade.stderr
    assert "DROP TABLE calendar_operation_attempt" in offline_downgrade.stdout

    try:
        _run_alembic(mysql_test_url, "downgrade", "0009_ics_export_persistence")
        _run_alembic(mysql_test_url, "upgrade", "0010_calendar_operation_persistence")

        async def live_contract() -> dict[str, dict[str, str]]:
            async with mysql_test_database.session_factory() as session:
                values: dict[str, dict[str, str]] = {}
                for name in expected_columns:
                    columns = (
                        await session.execute(text(f"SHOW COLUMNS FROM {name}"))
                    ).all()
                    values[name] = {str(row[0]): str(row[1]).lower() for row in columns}
                return values

        live = asyncio.run(live_contract())
        for name, columns in expected_columns.items():
            assert set(live[name]) == columns
        assert live["calendar_operation_draft"]["created_at"] == "datetime(6)"
        assert live["calendar_operation_item"]["operation_key"] == "varchar(64)"
        _run_alembic(mysql_test_url, "downgrade", "0009_ics_export_persistence")
    finally:
        _run_alembic(mysql_test_url, "upgrade", "head")
    assert (
        "0012_recovery_application_persistence"
        in _run_alembic(mysql_test_url, "current").stdout
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_operation_draft_round_trip_order_user_isolation_and_cas(
    mysql_test_database: Database,
) -> None:
    first_user = replace(
        make_user(), email=f"calendar-draft-{uuid4().hex}@fitweek.test"
    )
    second_user = replace(
        make_user(), email=f"calendar-other-{uuid4().hex}@fitweek.test"
    )
    users = MySQLUserAccountRepository(mysql_test_database.session_factory)
    repository = MySQLCalendarOperationRepository(mysql_test_database.session_factory)
    root_plan_id = uuid4()
    binding = _binding(first_user.id, root_plan_id, uuid4(), "draft")
    draft = _draft(first_user.id, root_plan_id, binding_id=binding.id)
    await users.save(first_user)
    await users.save(second_user)
    try:
        await repository.save_binding(binding)
        assert await repository.save_draft(draft) == draft
        stored = await repository.get_draft(first_user.id, draft.id)
        assert stored == draft
        assert tuple(item.id for item in stored.items) == tuple(
            item.id for item in draft.items
        )
        assert await repository.get_draft(second_user.id, draft.id) is None
        assert (
            await repository.get_by_request(second_user.id, draft.client_request_id)
            is None
        )

        approved = draft.approve(TEST_NOW + timedelta(minutes=1))
        left, right = await asyncio.gather(
            repository.update_draft(approved),
            repository.update_draft(approved),
            return_exceptions=True,
        )
        assert sum(value == approved for value in (left, right)) == 1
        assert (
            sum(isinstance(value, RepositoryConflictError) for value in (left, right))
            == 1
        )
    finally:
        await _cleanup(mysql_test_database, (first_user.id, second_user.id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_binding_attempt_round_trip_uniqueness_and_user_isolation(
    mysql_test_database: Database,
) -> None:
    first_user = replace(
        make_user(), email=f"calendar-binding-{uuid4().hex}@fitweek.test"
    )
    second_user = replace(
        make_user(), email=f"calendar-binding-other-{uuid4().hex}@fitweek.test"
    )
    users = MySQLUserAccountRepository(mysql_test_database.session_factory)
    repository = MySQLCalendarOperationRepository(mysql_test_database.session_factory)
    root_plan_id, session_id = uuid4(), uuid4()
    binding = _binding(first_user.id, root_plan_id, session_id, "binding")
    draft = _draft(first_user.id, root_plan_id, binding_id=binding.id)
    await users.save(first_user)
    await users.save(second_user)
    try:
        assert await repository.save_binding(binding) == binding
        assert await repository.get_binding(first_user.id, binding.id) == binding
        assert await repository.get_binding(second_user.id, binding.id) is None
        assert await repository.list_bindings_for_plan(first_user.id, root_plan_id) == (
            binding,
        )
        assert (
            await repository.list_bindings(
                second_user.id, binding.provider, binding.calendar_id, root_plan_id
            )
            == ()
        )
        updated = replace(
            binding,
            last_payload_fingerprint="c" * 64,
            updated_at=TEST_NOW + timedelta(seconds=1),
            version=2,
        )
        assert await repository.save_binding(updated) == updated
        with pytest.raises(RepositoryUniqueError, match="calendar_binding.key"):
            await repository.save_binding(replace(binding, id=uuid4()))
        assert await repository.get_binding(first_user.id, binding.id) == updated

        await repository.save_draft(draft)
        attempt = CalendarOperationAttempt(
            id=uuid4(),
            user_id=first_user.id,
            draft_id=draft.id,
            item_id=draft.items[0].id,
            attempt_no=1,
            outcome=CalendarAttemptOutcome.SUCCEEDED,
            error_code=None,
            response_reference_hash="a" * 64,
            started_at=TEST_NOW,
            finished_at=TEST_NOW,
        )
        assert await repository.save_attempt(attempt) == attempt
        assert await repository.list_attempts(first_user.id, draft.id) == (attempt,)
        assert await repository.list_attempts(second_user.id, draft.id) == ()
        with pytest.raises(RepositoryUniqueError, match="calendar_attempt.number"):
            await repository.save_attempt(replace(attempt, id=uuid4()))
        assert await repository.list_attempts(first_user.id, draft.id) == (attempt,)
    finally:
        await _cleanup(mysql_test_database, (first_user.id, second_user.id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_idempotent_replay_returns_persisted_aggregate_not_caller_value(  # noqa: E501
    mysql_test_database: Database,
) -> None:
    user = replace(make_user(), email=f"calendar-replay-{uuid4().hex}@fitweek.test")
    repository = MySQLCalendarOperationRepository(mysql_test_database.session_factory)
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
    stored = _draft(user.id, uuid4())
    caller_only = _draft(
        user.id, stored.root_plan_id, request_id=stored.client_request_id
    )
    caller_only = replace(
        caller_only,
        request_fingerprint=stored.request_fingerprint,
    )
    try:
        assert await repository.save_draft(stored) == stored
        replayed = await repository.save_draft(caller_only)
        assert replayed == stored
        assert await repository.get_draft(user.id, stored.id) == stored
        assert await repository.get_draft(user.id, caller_only.id) is None
    finally:
        await _cleanup(mysql_test_database, (user.id,))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_binding_external_identity_and_uid_are_immutable(
    mysql_test_database: Database,
) -> None:
    user = replace(make_user(), email=f"calendar-identity-{uuid4().hex}@fitweek.test")
    repository = MySQLCalendarOperationRepository(mysql_test_database.session_factory)
    binding = _binding(user.id, uuid4(), uuid4(), "identity")
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
    try:
        assert await repository.save_binding(binding) == binding
        rewritten = replace(
            binding,
            external_event_id="event-rewritten",
            stable_uid="uid-rewritten",
            updated_at=TEST_NOW + timedelta(seconds=1),
            version=2,
        )
        with pytest.raises(RepositoryUniqueError, match="calendar_binding.immutable"):
            await repository.save_binding(rewritten)
        assert await repository.get_binding(user.id, binding.id) == binding
    finally:
        await _cleanup(mysql_test_database, (user.id,))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_save_draft_retries_only_retryable_lock_errors_and_converges(  # noqa: E501
    mysql_test_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only MySQL lock failures retry the complete aggregate transaction."""

    user = replace(make_user(), email=f"calendar-lock-{uuid4().hex}@fitweek.test")
    repository = MySQLCalendarOperationRepository(mysql_test_database.session_factory)
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
    original_flush = AsyncSession.flush

    def lock_error(code: int) -> OperationalError:
        return OperationalError(
            "controlled calendar aggregate save",
            {},
            Exception(code, "controlled MySQL lock category"),
        )

    async def assert_clean_retry(code: int) -> CalendarOperationDraft:
        draft = _draft(user.id, uuid4())
        attempts = 0
        sessions: list[AsyncSession] = []

        async def fail_once(
            self: AsyncSession, *args: object, **kwargs: object
        ) -> None:
            nonlocal attempts
            attempts += 1
            sessions.append(self)
            if attempts == 1:
                raise lock_error(code)
            await original_flush(self, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "flush", fail_once)
        assert await repository.save_draft(draft) == draft
        assert attempts == 2
        assert sessions[0] is not sessions[1]
        assert await repository.get_draft(user.id, draft.id) == draft
        async with mysql_test_database.session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CalendarOperationDraftModel)
                    .where(CalendarOperationDraftModel.id == str(draft.id))
                )
                == 1
            )
            assert await session.scalar(
                select(func.count())
                .select_from(CalendarOperationItemModel)
                .where(CalendarOperationItemModel.draft_id == str(draft.id))
            ) == len(draft.items)
        return draft

    try:
        await assert_clean_retry(1213)
        await assert_clean_retry(1205)

        exhausted = _draft(user.id, uuid4())
        exhausted_error = lock_error(1213)
        exhausted_attempts = 0
        exhausted_sessions: list[AsyncSession] = []

        async def always_locked(
            self: AsyncSession, *args: object, **kwargs: object
        ) -> None:
            del args, kwargs
            nonlocal exhausted_attempts
            exhausted_attempts += 1
            exhausted_sessions.append(self)
            raise exhausted_error

        monkeypatch.setattr(AsyncSession, "flush", always_locked)
        with pytest.raises(OperationalError) as exhausted_result:
            await repository.save_draft(exhausted)
        assert exhausted_result.value is exhausted_error
        assert exhausted_attempts == 3
        assert all(
            left is not right
            for index, left in enumerate(exhausted_sessions)
            for right in exhausted_sessions[index + 1 :]
        )
        assert await repository.get_draft(user.id, exhausted.id) is None

        rejected = _draft(user.id, uuid4())
        rejected_error = lock_error(1045)
        rejected_attempts = 0

        async def reject_once(
            self: AsyncSession, *args: object, **kwargs: object
        ) -> None:
            del self, args, kwargs
            nonlocal rejected_attempts
            rejected_attempts += 1
            raise rejected_error

        monkeypatch.setattr(AsyncSession, "flush", reject_once)
        with pytest.raises(OperationalError) as rejected_result:
            await repository.save_draft(rejected)
        assert rejected_result.value is rejected_error
        assert rejected_attempts == 1
        assert await repository.get_draft(user.id, rejected.id) is None

        async with mysql_test_database.session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CalendarOperationDraftModel)
                    .where(
                        CalendarOperationDraftModel.id.in_(
                            (str(exhausted.id), str(rejected.id))
                        )
                    )
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CalendarOperationItemModel)
                    .where(
                        CalendarOperationItemModel.draft_id.in_(
                            (str(exhausted.id), str(rejected.id))
                        )
                    )
                )
                == 0
            )
    finally:
        monkeypatch.setattr(AsyncSession, "flush", original_flush)
        await _cleanup(mysql_test_database, (user.id,))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_aggregate_concurrency_and_flush_failure_rollback(
    mysql_test_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = replace(make_user(), email=f"calendar-race-{uuid4().hex}@fitweek.test")
    users = MySQLUserAccountRepository(mysql_test_database.session_factory)
    repository = MySQLCalendarOperationRepository(mysql_test_database.session_factory)
    draft = _draft(user.id, uuid4())
    await users.save(user)
    original_flush = AsyncSession.flush
    failed = False

    async def fail_once(self: AsyncSession, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("controlled calendar aggregate flush failure")
        await original_flush(self, *args, **kwargs)

    try:
        monkeypatch.setattr(AsyncSession, "flush", fail_once)
        with pytest.raises(
            RuntimeError, match="controlled calendar aggregate flush failure"
        ):
            await repository.save_draft(draft)
        monkeypatch.setattr(AsyncSession, "flush", original_flush)
        assert await repository.get_draft(user.id, draft.id) is None
        first, second = await asyncio.gather(
            repository.save_draft(draft), repository.save_draft(draft)
        )
        assert first == second == draft
        async with mysql_test_database.session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CalendarOperationDraftModel)
                    .where(CalendarOperationDraftModel.id == str(draft.id))
                )
                == 1
            )
            assert await session.scalar(
                select(func.count())
                .select_from(CalendarOperationItemModel)
                .where(CalendarOperationItemModel.draft_id == str(draft.id))
            ) == len(draft.items)
    finally:
        monkeypatch.setattr(AsyncSession, "flush", original_flush)
        await _cleanup(mysql_test_database, (user.id,))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_reconciliation_persists_create_keep_update_delete_without_provider_call(  # noqa: E501
    mysql_test_database: Database,
) -> None:  # noqa: E501
    user = replace(make_user(), email=f"calendar-reconcile-{uuid4().hex}@fitweek.test")
    raw_plan = make_plan(
        user_id=user.id, session_count=5, status=WeeklyPlanStatus.CONFIRMED
    )
    future = raw_plan.sessions[2]
    cancelled = replace(raw_plan.sessions[3], status=WorkoutSessionStatus.CANCELLED)
    past = replace(
        raw_plan.sessions[4],
        scheduled_start=TEST_NOW - timedelta(days=2),
        scheduled_end=TEST_NOW - timedelta(days=2) + timedelta(minutes=30),
    )
    plan = replace(
        raw_plan,
        sessions=(raw_plan.sessions[0], raw_plan.sessions[1], future, cancelled, past),
    )
    users = MySQLUserAccountRepository(mysql_test_database.session_factory)
    plans = MySQLPlanRepository(mysql_test_database.session_factory)
    operations = MySQLCalendarOperationRepository(mysql_test_database.session_factory)
    policy = CalendarPayloadPolicy()
    keep_payload = policy.build(
        user_id=user.id, plan=plan, session=plan.sessions[0], timezone="UTC"
    )
    keep = replace(
        _binding(user.id, plan.series_id, plan.sessions[0].id, "keep"),
        last_payload_fingerprint=keep_payload.payload_fingerprint,
    )
    changed = _binding(user.id, plan.series_id, plan.sessions[1].id, "changed")
    orphan = _binding(user.id, plan.series_id, uuid4(), "orphan")
    await users.save(user)
    await plans.save(plan)
    try:
        for binding in (keep, changed, orphan):
            await operations.save_binding(binding)
        service = CalendarOperationService(
            plans=plans,
            operations=operations,
            gateway=CalendarWriteGateway(
                provider=None, enabled=False, timeout_seconds=1, max_attempts=1
            ),
            clock=FakeClock(TEST_NOW),
        )
        command = CreateCalendarOperationCommand(
            client_request_id=f"calendar-reconcile-{uuid4().hex}",
            expected_plan_version=plan.version,
            provider="test-calendar",
            calendar_id="calendar-0017",
        )
        draft, created = await service.create_draft(
            user, plan.series_id, plan.revision, command
        )
        assert created
        by_session = {item.session_id: item for item in draft.items}
        assert (
            by_session[plan.sessions[0].id].operation_type is CalendarOperationType.KEEP
        )
        assert (
            by_session[plan.sessions[0].id].status
            is CalendarOperationItemStatus.SKIPPED
        )
        assert (
            by_session[plan.sessions[1].id].operation_type
            is CalendarOperationType.UPDATE
        )
        assert by_session[future.id].operation_type is CalendarOperationType.CREATE
        assert (
            by_session[orphan.session_id].operation_type is CalendarOperationType.DELETE
        )
        assert cancelled.id not in by_session
        assert past.id not in by_session
        replayed, replayed_created = await service.create_draft(
            user, plan.series_id, plan.revision, command
        )
        assert replayed == draft
        assert not replayed_created
        with pytest.raises(CalendarOperationIdempotencyConflict):
            await service.create_draft(
                user,
                plan.series_id,
                plan.revision,
                replace(command, calendar_id="calendar-0017-changed"),
            )
        assert service._gateway.provider_name == "none"
    finally:
        await _cleanup(mysql_test_database, (user.id,), (plan.id,))
