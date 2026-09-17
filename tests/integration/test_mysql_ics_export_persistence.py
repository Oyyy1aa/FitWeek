"""MySQL ICS export persistence contract tests."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.common import RepositoryUniqueError
from app.domain.ics.models import IcsExportRecord, IcsExportResult
from app.persistence.database import Database
from app.persistence.mysql.ics_export_repository import MySQLIcsExportRepository
from app.persistence.mysql.models import Base, IcsExportModel, UserAccountModel
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.factories import make_user


def _record(
    *,
    user_id: UUID,
    export_id: UUID | None = None,
    request_id: str | None = None,
    content: bytes = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
) -> IcsExportRecord:
    return IcsExportRecord(
        result=IcsExportResult(
            id=export_id or uuid4(),
            user_id=user_id,
            client_request_id=request_id or f"ics-export-{uuid4().hex}",
            request_fingerprint=hashlib.sha256(content + b"request").hexdigest(),
            root_plan_id=uuid4(),
            revision=2,
            plan_version=3,
            policy_version="ics-policy-v1",
            content_sha256=hashlib.sha256(content).hexdigest(),
            event_count=1,
            byte_size=len(content),
            filename="fitweek-plan.ics",
            created_at=datetime(2026, 7, 29, 0, 0, tzinfo=UTC),
        ),
        content=content,
    )


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


async def _cleanup(database: Database, user_ids: tuple[UUID, ...]) -> None:
    if not user_ids:
        return
    values = [str(value) for value in user_ids]
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(IcsExportModel).where(IcsExportModel.user_id.in_(values))
            )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id.in_(values))
            )


def test_ics_export_migration_offline_online_and_schema_contract(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    """The durable ICS export fact is represented in MySQL metadata."""

    assert "ics_export" in Base.metadata.tables
    table = Base.metadata.tables["ics_export"]
    assert set(table.columns.keys()) == {
        "id",
        "user_id",
        "client_request_id",
        "request_fingerprint",
        "root_plan_id",
        "revision",
        "plan_version",
        "policy_version",
        "content_sha256",
        "event_count",
        "byte_size",
        "filename",
        "content",
        "created_at",
    }
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("user_id", "client_request_id") in unique_columns
    assert "ix_ics_export_user_plan_revision" in {index.name for index in table.indexes}

    offline_upgrade = _run_offline(
        "upgrade", "0008_orchestration_status_capacity:0009_ics_export_persistence"
    )
    assert offline_upgrade.returncode == 0, offline_upgrade.stderr
    assert "CREATE TABLE ics_export" in offline_upgrade.stdout
    assert "LONGBLOB" in offline_upgrade.stdout
    offline_downgrade = _run_offline(
        "downgrade", "0009_ics_export_persistence:0008_orchestration_status_capacity"
    )
    assert offline_downgrade.returncode == 0, offline_downgrade.stderr
    assert "DROP TABLE ics_export" in offline_downgrade.stdout

    try:
        _run_alembic(mysql_test_url, "downgrade", "0008_orchestration_status_capacity")
        _run_alembic(mysql_test_url, "upgrade", "0009_ics_export_persistence")

        async def live_contract() -> tuple[set[str], dict[str, str], set[str]]:
            async with mysql_test_database.session_factory() as session:
                columns = (
                    await session.execute(text("SHOW COLUMNS FROM ics_export"))
                ).all()
                indexes = (
                    await session.execute(text("SHOW INDEX FROM ics_export"))
                ).all()
                return (
                    {str(row[0]) for row in columns},
                    {str(row[0]): str(row[1]).lower() for row in columns},
                    {str(row[2]) for row in indexes},
                )

        names, types, indexes = asyncio.run(live_contract())
        assert set(table.columns.keys()) == names
        assert types["content"] == "longblob"
        assert {
            "uq_ics_export_user_request",
            "ix_ics_export_user_plan_revision",
        } <= indexes
        _run_alembic(mysql_test_url, "downgrade", "0008_orchestration_status_capacity")
        _run_alembic(mysql_test_url, "upgrade", "head")
        current = _run_alembic(mysql_test_url, "current")
        head_revision = _run_alembic(mysql_test_url, "heads").stdout.split()[0]
        assert head_revision in current.stdout
    finally:
        _run_alembic(mysql_test_url, "upgrade", "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_ics_export_repository_round_trip_preserves_exact_bytes_and_user_isolation(  # noqa: E501
    mysql_test_database: Database,
) -> None:
    first_user = replace(make_user(), email=f"ics-first-{uuid4().hex}@fitweek.test")
    second_user = replace(make_user(), email=f"ics-second-{uuid4().hex}@fitweek.test")
    repository = MySQLIcsExportRepository(mysql_test_database.session_factory)
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(
        first_user
    )
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(
        second_user
    )
    record = _record(user_id=first_user.id, content=b"synthetic\x00ics\r\n")
    try:
        stored = await repository.save(record)
        assert stored == record
        assert await repository.get(first_user.id, record.result.id) == record
        assert (
            await repository.get_by_request(
                first_user.id, record.result.client_request_id
            )
            == record
        )
        assert await repository.get(second_user.id, record.result.id) is None
        assert (
            await repository.get_by_request(
                second_user.id, record.result.client_request_id
            )
            is None
        )
        assert stored.content == record.content
        assert stored.result.content_sha256 == record.result.content_sha256
        assert stored.result.byte_size == len(record.content)
    finally:
        await _cleanup(mysql_test_database, (first_user.id, second_user.id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_ics_export_repository_concurrent_idempotency_and_conflict(
    mysql_test_database: Database,
) -> None:
    first_user = replace(
        make_user(), email=f"ics-concurrent-{uuid4().hex}@fitweek.test"
    )
    second_user = replace(
        make_user(), email=f"ics-collision-{uuid4().hex}@fitweek.test"
    )
    repository = MySQLIcsExportRepository(mysql_test_database.session_factory)
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(
        first_user
    )
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(
        second_user
    )
    record = _record(user_id=first_user.id)
    try:
        first, second = await asyncio.gather(
            repository.save(record), repository.save(record)
        )
        assert first == second == record
        changed = replace(
            record,
            result=replace(record.result, request_fingerprint="f" * 64),
        )
        with pytest.raises(RepositoryUniqueError, match="ics_export.user_request"):
            await repository.save(changed)
        collision = _record(
            user_id=second_user.id,
            export_id=record.result.id,
        )
        with pytest.raises(RepositoryUniqueError, match="ics_export.id"):
            await repository.save(collision)
    finally:
        await _cleanup(mysql_test_database, (first_user.id, second_user.id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_ics_export_repository_failure_rolls_back_without_partial_row(
    mysql_test_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = replace(make_user(), email=f"ics-flush-failure-{uuid4().hex}@fitweek.test")
    repository = MySQLIcsExportRepository(mysql_test_database.session_factory)
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
    record = _record(user_id=user.id, content=b"fault-injection-ics\r\n")
    original_flush = AsyncSession.flush
    failed = False

    async def fail_once(self: AsyncSession, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("controlled flush failure")
        await original_flush(self, *args, **kwargs)

    try:
        monkeypatch.setattr(AsyncSession, "flush", fail_once)
        with pytest.raises(RuntimeError, match="controlled flush failure"):
            await repository.save(record)
        monkeypatch.setattr(AsyncSession, "flush", original_flush)
        assert await repository.get(user.id, record.result.id) is None
        assert await repository.save(record) == record
        assert await repository.get(user.id, record.result.id) == record
    finally:
        monkeypatch.setattr(AsyncSession, "flush", original_flush)
        await _cleanup(mysql_test_database, (user.id,))
