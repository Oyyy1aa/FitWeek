"""MySQL orchestration runtime composition contracts."""

import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select

import app.api.health as health_module
import app.main as main_module
from app.config import PersistenceBackend, Settings
from app.orchestration.mysql_runtime import MySQLOrchestrationRuntime
from app.orchestration.worker_pool import OrchestrationWorkerPool
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AgentStepModel,
    AuditEventModel,
    CheckpointModel,
    ContextSnapshotModel,
    FitnessProfileModel,
    PlanningRunModel,
    SessionExerciseModel,
    StepDependencyModel,
    UserAccountModel,
    UserConstraintModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)
from app.persistence.mysql.orchestration_repository import MySQLOrchestrationRepository
from tests.api.helpers import generation_payload, profile_payload

pytestmark = pytest.mark.integration


def _mysql_settings(mysql_test_url: str, email: str) -> Settings:
    return Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(mysql_test_url),
        redis_enabled=False,
        orchestrator_enabled=True,
        single_user_email=email,
        _env_file=None,
    )


async def _run_worker_once(
    environment: dict[str, str], worker_id: str
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.orchestration.cli",
        "worker",
        "--once",
        "--worker-id",
        worker_id,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError:
        process.terminate()
        await process.communicate()
        raise
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def _mysql_row_counts(database: Database, run_id: str) -> tuple[int, ...]:
    async with database.session_factory() as session:
        return (
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(PlanningRunModel)
                    .where(PlanningRunModel.id == run_id)
                )
                or 0
            ),
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(AgentStepModel)
                    .where(AgentStepModel.run_id == run_id)
                )
                or 0
            ),
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(CheckpointModel)
                    .where(CheckpointModel.run_id == run_id)
                )
                or 0
            ),
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEventModel)
                    .where(AuditEventModel.run_id == run_id)
                )
                or 0
            ),
        )


async def _delete_isolated_user(database: Database, user_id: str) -> None:
    """Delete exactly the durable graph owned by one random test user."""

    if not user_id:
        return
    async with database.session_factory() as session:
        async with session.begin():
            run_ids = (
                await session.scalars(
                    select(PlanningRunModel.id).where(
                        PlanningRunModel.user_id == user_id
                    )
                )
            ).all()
            step_ids = (
                await session.scalars(
                    select(AgentStepModel.id).where(AgentStepModel.run_id.in_(run_ids))
                )
            ).all()
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id == user_id)
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
            profile_ids = (
                await session.scalars(
                    select(FitnessProfileModel.id).where(
                        FitnessProfileModel.user_id == user_id
                    )
                )
            ).all()
            await session.execute(
                delete(ContextSnapshotModel).where(
                    ContextSnapshotModel.user_id == user_id
                )
            )
            await session.execute(
                delete(UserConstraintModel).where(
                    UserConstraintModel.profile_id.in_(profile_ids)
                )
            )
            await session.execute(
                delete(FitnessProfileModel).where(
                    FitnessProfileModel.id.in_(profile_ids)
                )
            )
            plan_ids = (
                await session.scalars(
                    select(WeeklyPlanModel.id).where(WeeklyPlanModel.user_id == user_id)
                )
            ).all()
            session_ids = (
                await session.scalars(
                    select(WorkoutSessionModel.id).where(
                        WorkoutSessionModel.plan_id.in_(plan_ids)
                    )
                )
            ).all()
            await session.execute(
                delete(SessionExerciseModel).where(
                    SessionExerciseModel.session_id.in_(session_ids)
                )
            )
            await session.execute(
                delete(WorkoutSessionModel).where(
                    WorkoutSessionModel.plan_id.in_(plan_ids)
                )
            )
            await session.execute(
                delete(WeeklyPlanModel).where(WeeklyPlanModel.id.in_(plan_ids))
            )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id == user_id)
            )


@pytest.mark.asyncio
async def test_mysql_runtime_uses_no_inmemory_orchestration_repository(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _mysql_settings(
        mysql_test_url, f"runtime-boundary-{uuid4().hex}@fitweek.test"
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    application = main_module.create_application()

    async with application.router.lifespan_context(application):
        runtime = application.state.mysql_orchestration_runtime
        assert isinstance(runtime, MySQLOrchestrationRuntime)
        assert isinstance(
            application.state.orchestration_repository,
            MySQLOrchestrationRepository,
        )
        assert application.state.orchestration_repository is runtime.repository
        assert application.state.orchestration_service is runtime.service
        assert not any(
            isinstance(value, OrchestrationWorkerPool)
            for value in application.state._state.values()
        )
        assert not any(
            task.get_name().startswith("fitweek-orchestrator-")
            for task in asyncio.all_tasks()
        )


@pytest.mark.asyncio
async def test_mysql_readiness_reports_external_worker_mode_truthfully(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _mysql_settings(
        mysql_test_url, f"runtime-ready-{uuid4().hex}@fitweek.test"
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    application = main_module.create_application()
    application.dependency_overrides[health_module.get_settings] = lambda: settings

    try:
        async with application.router.lifespan_context(application):
            runtime = application.state.mysql_orchestration_runtime
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                ready = await client.get("/health/ready")
                assert ready.status_code == 200
                assert ready.json()["status"] == "ready"
                assert ready.json()["checks"] == {
                    "mysql": "ok",
                    "redis": "disabled",
                    "orchestrator": "external_worker",
                }
                application.state.mysql_orchestration_runtime = None
                try:
                    unavailable = await client.get("/health/ready")
                    assert unavailable.status_code == 503
                    assert unavailable.json()["checks"]["orchestrator"] == "unavailable"
                finally:
                    application.state.mysql_orchestration_runtime = runtime
    finally:
        application.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_mysql_api_reads_durable_run_steps_checkpoints_and_audit(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = f"runtime-readback-{uuid4().hex}@fitweek.test"
    settings = _mysql_settings(mysql_test_url, email)
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    environment = os.environ.copy()
    environment.update(
        {
            "PERSISTENCE_BACKEND": "mysql",
            "DATABASE_URL": mysql_test_url,
            "ORCHESTRATOR_ENABLED": "true",
            "REDIS_ENABLED": "false",
            "SINGLE_USER_EMAIL": email,
        }
    )
    application_a = main_module.create_application()
    user_id = ""
    run_id = ""
    try:
        async with application_a.router.lifespan_context(application_a):
            async with AsyncClient(
                transport=ASGITransport(app=application_a), base_url="http://test"
            ) as client_a:
                user_id = (await client_a.get("/api/v1/users/me")).json()["id"]
                assert (
                    await client_a.put("/api/v1/profiles/me", json=profile_payload())
                ).status_code == 200
                created = await client_a.post(
                    "/api/v1/planning-runs",
                    json={
                        "client_request_id": f"runtime-readback-{uuid4().hex}",
                        **generation_payload(),
                    },
                )
                assert created.status_code == 202, created.text
                run_id = created.json()["run_id"]
                initial_steps = await client_a.get(
                    f"/api/v1/planning-runs/{run_id}/steps"
                )
                assert [item["status"] for item in initial_steps.json()] == ["READY"]

        worker_code, worker_stdout, worker_stderr = await _run_worker_once(
            environment, "runtime-readback-worker"
        )
        assert worker_code == 0, worker_stderr
        assert worker_stdout.strip() == "SUCCEEDED"
        counts_before = await _mysql_row_counts(mysql_test_database, run_id)
        assert counts_before[:3] == (1, 2, 1)

        application_b = main_module.create_application()
        assert application_b is not application_a
        assert application_b.state is not application_a.state
        async with application_b.router.lifespan_context(application_b):
            async with AsyncClient(
                transport=ASGITransport(app=application_b), base_url="http://test"
            ) as client_b:
                run = await client_b.get(f"/api/v1/planning-runs/{run_id}")
                steps = await client_b.get(f"/api/v1/planning-runs/{run_id}/steps")
                checkpoints = await client_b.get(
                    f"/api/v1/planning-runs/{run_id}/checkpoints"
                )
                audit = await client_b.get(f"/api/v1/planning-runs/{run_id}/audit")
                assert (
                    run.status_code
                    == steps.status_code
                    == checkpoints.status_code
                    == audit.status_code
                    == 200
                )
                assert run.json()["id"] == run_id
                assert [item["status"] for item in steps.json()] == [
                    "SUCCEEDED",
                    "READY",
                ]
                assert len(checkpoints.json()) == 1
                assert checkpoints.json()[0]["step_id"] == steps.json()[0]["id"]
                assert [item["sequence_no"] for item in audit.json()] == list(
                    range(1, len(audit.json()) + 1)
                )
                assert audit.json()
                payloads_before_repeat = (
                    run.json(),
                    steps.json(),
                    checkpoints.json(),
                    audit.json(),
                )
                repeated_payloads = (
                    (await client_b.get(f"/api/v1/planning-runs/{run_id}")).json(),
                    (
                        await client_b.get(f"/api/v1/planning-runs/{run_id}/steps")
                    ).json(),
                    (
                        await client_b.get(
                            f"/api/v1/planning-runs/{run_id}/checkpoints"
                        )
                    ).json(),
                    (
                        await client_b.get(f"/api/v1/planning-runs/{run_id}/audit")
                    ).json(),
                )
                assert repeated_payloads == payloads_before_repeat
                assert (
                    await _mysql_row_counts(mysql_test_database, run_id)
                    == counts_before
                )
    finally:
        await _delete_isolated_user(mysql_test_database, user_id)


@pytest.mark.asyncio
async def test_mysql_api_enqueues_run_without_inprocess_worker(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = f"runtime-{uuid4().hex}@fitweek.test"
    settings = Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(mysql_test_url),
        redis_enabled=False,
        orchestrator_enabled=True,
        single_user_email=email,
        _env_file=None,
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    application = main_module.create_application()
    user_id = ""
    try:
        async with application.router.lifespan_context(application):
            assert application.state.mysql_orchestration_runtime.worker is not None
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                user_id = (await client.get("/api/v1/users/me")).json()["id"]
                response = await client.post(
                    "/api/v1/planning-runs",
                    json={
                        "client_request_id": f"runtime-{uuid4().hex}",
                        **generation_payload(),
                    },
                )
                assert response.status_code == 202, response.text
                run_id = response.json()["run_id"]
                steps = await client.get(f"/api/v1/planning-runs/{run_id}/steps")
                assert [step["status"] for step in steps.json()] == ["READY"]
    finally:
        async with mysql_test_database.session_factory() as session:
            async with session.begin():
                run_ids = (
                    await session.scalars(
                        select(PlanningRunModel.id).where(
                            PlanningRunModel.user_id == user_id
                        )
                    )
                ).all()
                step_ids = (
                    await session.scalars(
                        select(AgentStepModel.id).where(
                            AgentStepModel.run_id.in_(run_ids)
                        )
                    )
                ).all()
                await session.execute(
                    delete(AuditEventModel).where(AuditEventModel.user_id == user_id)
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
                profile_ids = (
                    await session.scalars(
                        select(FitnessProfileModel.id).where(
                            FitnessProfileModel.user_id == user_id
                        )
                    )
                ).all()
                await session.execute(
                    delete(ContextSnapshotModel).where(
                        ContextSnapshotModel.user_id == user_id
                    )
                )
                await session.execute(
                    delete(UserConstraintModel).where(
                        UserConstraintModel.profile_id.in_(profile_ids)
                    )
                )
                await session.execute(
                    delete(FitnessProfileModel).where(
                        FitnessProfileModel.id.in_(profile_ids)
                    )
                )
                plan_ids = (
                    await session.scalars(
                        select(WeeklyPlanModel.id).where(
                            WeeklyPlanModel.user_id == user_id
                        )
                    )
                ).all()
                session_ids = (
                    await session.scalars(
                        select(WorkoutSessionModel.id).where(
                            WorkoutSessionModel.plan_id.in_(plan_ids)
                        )
                    )
                ).all()
                await session.execute(
                    delete(SessionExerciseModel).where(
                        SessionExerciseModel.session_id.in_(session_ids)
                    )
                )
                await session.execute(
                    delete(WorkoutSessionModel).where(
                        WorkoutSessionModel.plan_id.in_(plan_ids)
                    )
                )
                await session.execute(
                    delete(WeeklyPlanModel).where(WeeklyPlanModel.id.in_(plan_ids))
                )
                await session.execute(
                    delete(UserAccountModel).where(UserAccountModel.id == user_id)
                )


def test_worker_cli_rejects_memory_backend_and_redacts_startup_failure() -> None:
    environment = os.environ.copy()
    environment["PERSISTENCE_BACKEND"] = "memory"
    environment["ORCHESTRATOR_ENABLED"] = "true"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.orchestration.cli",
            "worker",
            "--once",
            "--worker-id",
            "test",
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 2
    assert "RuntimeError" in result.stderr
    assert "://" not in result.stderr


@pytest.mark.asyncio
async def test_worker_cli_once_executes_exactly_one_step(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = f"cli-{uuid4().hex}@fitweek.test"
    settings = Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(mysql_test_url),
        redis_enabled=False,
        orchestrator_enabled=True,
        single_user_email=email,
        _env_file=None,
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    environment = os.environ.copy()
    environment.update(
        {
            "PERSISTENCE_BACKEND": "mysql",
            "DATABASE_URL": mysql_test_url,
            "ORCHESTRATOR_ENABLED": "true",
            "REDIS_ENABLED": "false",
            "SINGLE_USER_EMAIL": email,
        }
    )
    application = main_module.create_application()
    user_id = ""
    try:
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                user_id = (await client.get("/api/v1/users/me")).json()["id"]
                assert (
                    await client.put("/api/v1/profiles/me", json=profile_payload())
                ).status_code == 200
                created = await client.post(
                    "/api/v1/planning-runs",
                    json={
                        "client_request_id": f"cli-{uuid4().hex}",
                        **generation_payload(),
                    },
                )
                assert created.status_code == 202
                run_id = created.json()["run_id"]
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.orchestration.cli",
                    "worker",
                    "--once",
                    "--worker-id",
                    "cli-test",
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
                assert process.returncode == 0, stderr.decode()
                assert stdout.decode().strip() == "SUCCEEDED"
                steps = await client.get(f"/api/v1/planning-runs/{run_id}/steps")
                assert [step["status"] for step in steps.json()] == [
                    "SUCCEEDED",
                    "READY",
                ]
                for worker_id in ("cli-plan", "cli-safety", "cli-wait"):
                    next_process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "app.orchestration.cli",
                        "worker",
                        "--once",
                        "--worker-id",
                        worker_id,
                        env=environment,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    next_stdout, next_stderr = await next_process.communicate()
                    assert next_process.returncode == 0, next_stderr.decode()
                    assert next_stdout.decode().strip() in {"SUCCEEDED", "WAITING_USER"}
                waiting = await client.get(f"/api/v1/planning-runs/{run_id}")
                assert waiting.json()["status"] == "WAITING_CONFIRMATION"
                plan_id = waiting.json()["result_reference"]
                plan = await client.get(f"/api/v1/plans/{plan_id}")
                confirmed = await client.post(
                    f"/api/v1/planning-runs/{run_id}/confirm",
                    json={"expected_plan_version": plan.json()["version"]},
                )
                assert confirmed.status_code == 200, confirmed.text
                final_process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.orchestration.cli",
                    "worker",
                    "--once",
                    "--worker-id",
                    "cli-finalize",
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                final_stdout, final_stderr = await final_process.communicate()
                assert final_process.returncode == 0, final_stderr.decode()
                assert final_stdout.decode().strip() == "SUCCEEDED"
                completed = await client.get(f"/api/v1/planning-runs/{run_id}")
                assert completed.json()["status"] == "COMPLETED"
                checkpoints = await client.get(
                    f"/api/v1/planning-runs/{run_id}/checkpoints"
                )
                assert len(checkpoints.json()) == 5
    finally:
        async with mysql_test_database.session_factory() as session:
            async with session.begin():
                run_ids = (
                    await session.scalars(
                        select(PlanningRunModel.id).where(
                            PlanningRunModel.user_id == user_id
                        )
                    )
                ).all()
                step_ids = (
                    await session.scalars(
                        select(AgentStepModel.id).where(
                            AgentStepModel.run_id.in_(run_ids)
                        )
                    )
                ).all()
                await session.execute(
                    delete(AuditEventModel).where(AuditEventModel.user_id == user_id)
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
                profile_ids = (
                    await session.scalars(
                        select(FitnessProfileModel.id).where(
                            FitnessProfileModel.user_id == user_id
                        )
                    )
                ).all()
                await session.execute(
                    delete(ContextSnapshotModel).where(
                        ContextSnapshotModel.user_id == user_id
                    )
                )
                await session.execute(
                    delete(UserConstraintModel).where(
                        UserConstraintModel.profile_id.in_(profile_ids)
                    )
                )
                await session.execute(
                    delete(FitnessProfileModel).where(
                        FitnessProfileModel.id.in_(profile_ids)
                    )
                )
                plan_ids = (
                    await session.scalars(
                        select(WeeklyPlanModel.id).where(
                            WeeklyPlanModel.user_id == user_id
                        )
                    )
                ).all()
                session_ids = (
                    await session.scalars(
                        select(WorkoutSessionModel.id).where(
                            WorkoutSessionModel.plan_id.in_(plan_ids)
                        )
                    )
                ).all()
                await session.execute(
                    delete(SessionExerciseModel).where(
                        SessionExerciseModel.session_id.in_(session_ids)
                    )
                )
                await session.execute(
                    delete(WorkoutSessionModel).where(
                        WorkoutSessionModel.plan_id.in_(plan_ids)
                    )
                )
                await session.execute(
                    delete(WeeklyPlanModel).where(WeeklyPlanModel.id.in_(plan_ids))
                )
                await session.execute(
                    delete(UserAccountModel).where(UserAccountModel.id == user_id)
                )
