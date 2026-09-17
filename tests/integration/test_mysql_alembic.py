"""Alembic upgrade/downgrade cycle against an isolated MySQL database."""

import os
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.domain.orchestration.enums import PlanningRunStatus, StepType
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AgentStepModel,
    AuditEventModel,
    CheckpointModel,
    PlanningRunModel,
    StepDependencyModel,
    UserAccountModel,
)
from app.persistence.mysql.orchestration_repository import (
    MySQLOrchestrationRepository,
)
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.factories import make_user
from tests.unit.orchestration.factories import NOW, make_run, make_step


def run_alembic(database_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def run_offline_alembic(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments, "--sql"],
        check=False,
        capture_output=True,
        text=True,
    )


def test_mysql_test_url_failure_representation_is_redacted(
    mysql_test_url: str,
) -> None:
    """Integration failures must not render the configured test credentials."""

    assert isinstance(mysql_test_url, str)
    assert type(mysql_test_url).__name__ == "RedactedTestDatabaseUrl"
    assert str(mysql_test_url)
    assert repr(mysql_test_url) == "<redacted test database url>"


async def table_names(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(text("SHOW TABLES"))
            return {str(row[0]) for row in rows}
    finally:
        await engine.dispose()


async def _seed_checkpoint(database: Database) -> tuple[UUID, UUID, UUID]:
    user = replace(make_user(), email=f"migration-{uuid4().hex}@fitweek.test")
    await MySQLUserAccountRepository(database.session_factory).save(user)
    repository = MySQLOrchestrationRepository(database.session_factory)
    run = make_run(
        user_id=user.id,
        client_request_id=f"migration-{uuid4().hex}",
    )
    step = make_step(run.id)
    await repository.create_run_with_initial_steps(run, (step,))
    claim = await repository.claim_next_step(
        worker_id="migration-worker",
        lease_duration=timedelta(seconds=10),
        now=NOW,
    )
    assert claim is not None
    await repository.complete_step(
        claim=claim,
        handler_version="migration-v1",
        output_payload={"safe": True},
        result_reference="migration-result",
        next_step_type=StepType.GENERATE_DETERMINISTIC_PLAN,
        run_status_after=PlanningRunStatus.GENERATING_SESSIONS,
        now=NOW,
    )
    return user.id, run.id, step.id


async def _cleanup_seeded_user(database: Database, user_id: UUID) -> None:
    async with database.session_factory() as session:
        async with session.begin():
            run_ids = (
                await session.scalars(
                    select(PlanningRunModel.id).where(
                        PlanningRunModel.user_id == str(user_id)
                    )
                )
            ).all()
            step_ids = (
                await session.scalars(
                    select(AgentStepModel.id).where(AgentStepModel.run_id.in_(run_ids))
                )
            ).all()
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id == str(user_id))
            )
            await session.execute(
                delete(CheckpointModel).where(CheckpointModel.run_id.in_(run_ids))
            )
            await session.execute(
                delete(StepDependencyModel).where(
                    StepDependencyModel.step_id.in_(step_ids)
                )
            )
            await session.execute(
                delete(AgentStepModel).where(AgentStepModel.run_id.in_(run_ids))
            )
            await session.execute(
                delete(PlanningRunModel).where(PlanningRunModel.id.in_(run_ids))
            )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id == str(user_id))
            )


def test_phase_2b_offline_migration_sql_is_deterministic_and_reversible() -> None:
    upgrade = run_offline_alembic(
        "upgrade", "0006_schedule_draft:0007_phase2b_orchestration_persistence"
    )
    assert upgrade.returncode == 0, upgrade.stderr
    assert "NoInspectionAvailable" not in upgrade.stderr
    assert "ALTER TABLE checkpoint RENAME TO planning_checkpoint" in upgrade.stdout
    assert "request_fingerprint" in upgrade.stdout
    assert "fencing_token" in upgrade.stdout

    downgrade = run_offline_alembic(
        "downgrade", "0007_phase2b_orchestration_persistence:0006_schedule_draft"
    )
    assert downgrade.returncode == 0, downgrade.stderr
    assert "NoInspectionAvailable" not in downgrade.stderr
    assert "ALTER TABLE planning_checkpoint RENAME TO checkpoint" in downgrade.stdout


def test_orchestration_status_capacity_offline_sql_is_reversible() -> None:
    upgrade_range = (
        "0007_phase2b_orchestration_persistence:0008_orchestration_status_capacity"
    )
    upgrade = run_offline_alembic("upgrade", upgrade_range)
    assert upgrade.returncode == 0, upgrade.stderr
    assert "NoInspectionAvailable" not in upgrade.stderr
    assert (
        "ALTER TABLE planning_run MODIFY status VARCHAR(64) NOT NULL" in upgrade.stdout
    )
    assert (
        "ALTER TABLE planning_checkpoint MODIFY run_status_after VARCHAR(64) NOT NULL"
        in upgrade.stdout
    )

    downgrade_range = (
        "0008_orchestration_status_capacity:0007_phase2b_orchestration_persistence"
    )
    downgrade = run_offline_alembic("downgrade", downgrade_range)
    assert downgrade.returncode == 0, downgrade.stderr
    assert "NoInspectionAvailable" not in downgrade.stderr
    assert (
        "ALTER TABLE planning_run MODIFY status VARCHAR(32) NOT NULL"
        in downgrade.stdout
    )
    assert (
        "ALTER TABLE planning_checkpoint MODIFY run_status_after VARCHAR(32) NOT NULL"
        in downgrade.stdout
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_alembic_upgrade_downgrade_cycle(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    run_alembic(mysql_test_url, "upgrade", "head")
    head_revision = run_alembic(mysql_test_url, "heads").stdout.split()[0]
    assert head_revision in run_alembic(mysql_test_url, "current").stdout
    user_id, run_id, step_id = await _seed_checkpoint(mysql_test_database)
    try:
        run_alembic(mysql_test_url, "downgrade", "0006_schedule_draft")
        legacy_tables = await table_names(mysql_test_url)
        assert "checkpoint" in legacy_tables
        assert "planning_checkpoint" not in legacy_tables

        run_alembic(mysql_test_url, "upgrade", "head")
        current = run_alembic(mysql_test_url, "current")
        assert head_revision in current.stdout
        assert {
            "user_account",
            "planning_run",
            "agent_step",
            "planning_checkpoint",
            "audit_event",
            "ics_export",
        } <= await table_names(mysql_test_url)
        checkpoints = await MySQLOrchestrationRepository(
            mysql_test_database.session_factory
        ).list_checkpoints(run_id)
        assert len(checkpoints) == 1
        assert checkpoints[0].step_id == step_id
    finally:
        await _cleanup_seeded_user(mysql_test_database, user_id)
