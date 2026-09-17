"""MySQL capacity contracts for durable orchestration Run statuses."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.domain.orchestration.enums import (
    PlanningRunStatus,
    StepType,
    WorkflowType,
)
from app.persistence.database import Database
from app.persistence.mysql.models import Base
from app.persistence.mysql.orchestration_repository import (
    MySQLOrchestrationRepository,
)
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.factories import make_user
from tests.integration.test_mysql_alembic import _cleanup_seeded_user, run_alembic
from tests.unit.orchestration.factories import NOW, make_run, make_step


async def _column_lengths(database: Database) -> dict[tuple[str, str], int]:
    async with database.session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT table_name, column_name, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "AND (table_name = 'planning_run' AND column_name = 'status' "
                "OR table_name = 'planning_checkpoint' "
                "AND column_name = 'run_status_after')"
            )
        )
        return {(str(row[0]), str(row[1])): int(row[2]) for row in rows}


async def _run_alembic_refusal(
    database_url: str, revision: str
) -> tuple[int, str, str]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "downgrade",
        revision,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    return process.returncode or 0, stdout.decode(), stderr.decode()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_planning_run_status_values_fit_model_and_database_schema(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    run_alembic(mysql_test_url, "upgrade", "head")
    capacity = Base.metadata.tables["planning_run"].c.status.type.length
    checkpoint_capacity = Base.metadata.tables[
        "planning_checkpoint"
    ].c.run_status_after.type.length
    assert capacity == checkpoint_capacity == 64
    assert max(len(value.value) for value in PlanningRunStatus) <= capacity
    lengths = await _column_lengths(mysql_test_database)
    assert lengths == {
        ("planning_run", "status"): 64,
        ("planning_checkpoint", "run_status_after"): 64,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_claim_persists_long_session_schedule_recovery_statuses(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    run_alembic(mysql_test_url, "upgrade", "head")
    user = replace(make_user(), email=f"status-capacity-{uuid4().hex}@fitweek.test")
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
    repository = MySQLOrchestrationRepository(mysql_test_database.session_factory)
    cases = (
        (
            WorkflowType.SESSION_DESIGN_PLAN_INTEGRATION,
            StepType.LOAD_SESSION_APPLICATION_CONTEXT,
            PlanningRunStatus.LOADING_SESSION_APPLICATION_CONTEXT,
        ),
        (
            WorkflowType.SCHEDULE_PLAN_INTEGRATION,
            StepType.LOAD_SCHEDULE_APPLICATION_CONTEXT,
            PlanningRunStatus.LOADING_SCHEDULE_APPLICATION_CONTEXT,
        ),
        (
            WorkflowType.RECOVERY_PLAN_INTEGRATION,
            StepType.LOAD_RECOVERY_APPLICATION_CONTEXT,
            PlanningRunStatus.LOADING_RECOVERY_APPLICATION_CONTEXT,
        ),
    )
    try:
        for workflow_type, step_type, expected_status in cases:
            run = replace(
                make_run(
                    user_id=user.id,
                    client_request_id=f"status-capacity-{uuid4().hex}",
                ),
                workflow_type=workflow_type,
            )
            await repository.create_run_with_initial_steps(
                run,
                (make_step(run.id, step_type=step_type),),
            )
            claim = await repository.claim_next_step(
                worker_id=f"status-capacity-{step_type.value}",
                lease_duration=timedelta(seconds=10),
                now=NOW,
            )
            assert claim is not None
            restored = await repository.get_run_for_user(run.id, user.id)
            assert restored is not None
            assert restored.status is expected_status
    finally:
        await _cleanup_seeded_user(mysql_test_database, user.id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_capacity_downgrade_refuses_lossy_narrowing(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    run_alembic(mysql_test_url, "upgrade", "head")
    user = replace(make_user(), email=f"status-lossy-{uuid4().hex}@fitweek.test")
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
    repository = MySQLOrchestrationRepository(mysql_test_database.session_factory)
    run = replace(
        make_run(user_id=user.id, client_request_id=f"status-lossy-{uuid4().hex}"),
        workflow_type=WorkflowType.SESSION_DESIGN_PLAN_INTEGRATION,
    )
    await repository.create_run_with_initial_steps(
        run,
        (make_step(run.id, step_type=StepType.LOAD_SESSION_APPLICATION_CONTEXT),),
    )
    claim = await repository.claim_next_step(
        worker_id="status-lossy-worker",
        lease_duration=timedelta(seconds=10),
        now=NOW,
    )
    assert claim is not None
    try:
        return_code, _stdout, stderr = await _run_alembic_refusal(
            mysql_test_url,
            "0007_phase2b_orchestration_persistence",
        )
        assert return_code != 0
        assert "Refusing lossy downgrade" in stderr
        assert (
            "0008_orchestration_status_capacity"
            in run_alembic(mysql_test_url, "current").stdout
        )
        assert await _column_lengths(mysql_test_database) == {
            ("planning_run", "status"): 64,
            ("planning_checkpoint", "run_status_after"): 64,
        }
        restored = await repository.get_run_for_user(run.id, user.id)
        assert restored is not None
        assert restored.status is PlanningRunStatus.LOADING_SESSION_APPLICATION_CONTEXT
    finally:
        await _cleanup_seeded_user(mysql_test_database, user.id)
        run_alembic(
            mysql_test_url,
            "downgrade",
            "0007_phase2b_orchestration_persistence",
        )
        run_alembic(mysql_test_url, "upgrade", "head")
