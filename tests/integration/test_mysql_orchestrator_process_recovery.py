"""Independent-process MySQL crash, reaper, and reclaim recovery contracts."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select, update

import app.main as main_module
from app.config import PersistenceBackend, Settings
from app.domain.orchestration.enums import PlanningRunStatus
from app.domain.orchestration.errors import StepLeaseLost
from app.domain.orchestration.models import ClaimedStep
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
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.api.helpers import generation_payload, profile_payload
from tests.factories import make_user
from tests.support.mysql_orchestrator_process import (
    cli_environment,
    reserve_unreachable_loopback_port,
    run_claim_then_exit,
    run_reaper_once,
    run_worker_once,
    signal_worker_loop_gracefully,
    start_api_process,
    start_worker_loop_process,
    start_worker_once_process,
    stop_api_process,
    stop_worker_process_for_cleanup,
    wait_for_worker_loop_start,
    wait_for_worker_process,
)
from tests.unit.orchestration.factories import make_run, make_step


async def _delete_isolated_user(database: Database, user_id: str) -> None:
    """Delete exactly the test user and rows transitively owned by it."""

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


async def _delete_previous_claim_test_rows(database: Database) -> None:
    """Recover only rows created by an interrupted run of this exact test node."""

    async with database.session_factory() as session:
        user_ids = (
            await session.scalars(
                select(UserAccountModel.id).where(
                    UserAccountModel.email.like("crash-%@fitweek.test")
                )
            )
        ).all()
    for user_id in user_ids:
        await _delete_isolated_user(database, user_id)


def test_reaper_cli_rejects_memory_backend_without_sensitive_output() -> None:
    environment = os.environ.copy()
    environment["PERSISTENCE_BACKEND"] = "memory"
    environment["ORCHESTRATOR_ENABLED"] = "true"
    result = subprocess.run(
        [sys.executable, "-m", "app.orchestration.cli", "reaper", "--once"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "://" not in result.stderr


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_then_exit_leaves_mysql_step_running_with_fence(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    await _delete_previous_claim_test_rows(mysql_test_database)
    user = replace(make_user(), email=f"crash-{uuid4().hex}@fitweek.test")
    foreign_user = replace(
        make_user(), email=f"crash-foreign-{uuid4().hex}@fitweek.test"
    )
    try:
        await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
        await MySQLUserAccountRepository(mysql_test_database.session_factory).save(
            foreign_user
        )
        repository = MySQLOrchestrationRepository(mysql_test_database.session_factory)
        foreign_run = make_run(
            user_id=foreign_user.id,
            client_request_id=f"crash-foreign-{uuid4().hex}",
        )
        foreign_now = datetime.now(UTC)
        foreign_step = replace(
            make_step(foreign_run.id),
            priority=100,
            next_execute_at=foreign_now,
            created_at=foreign_now,
            updated_at=foreign_now,
        )
        await repository.create_run_with_initial_steps(foreign_run, (foreign_step,))
        run = make_run(user_id=user.id, client_request_id=f"crash-{uuid4().hex}")
        now = datetime.now(UTC)
        step = replace(
            make_step(run.id), next_execute_at=now, created_at=now, updated_at=now
        )
        await repository.create_run_with_initial_steps(run, (step,))
        environment = cli_environment(database_url=mysql_test_url, email=user.email)
        returncode, stdout, stderr = await asyncio.wait_for(
            run_claim_then_exit(environment, "crash-child"), timeout=20
        )
        assert returncode == 0, stderr
        claim = json.loads(stdout)
        assert claim == {
            "run_id": str(run.id),
            "step_id": str(step.id),
            "fencing_token": 1,
        }
        stored = (await repository.list_steps(run.id))[0]
        assert stored.status.value == "RUNNING"
        assert stored.worker_id == "crash-child"
        assert stored.lease_expires_at is not None
        assert stored.fencing_token == claim["fencing_token"] > 0
        assert await repository.list_checkpoints(run.id) == []
    finally:
        await _delete_isolated_user(mysql_test_database, str(user.id))
        await _delete_isolated_user(mysql_test_database, str(foreign_user.id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_crash_reaper_and_new_process_resume_without_repeating_checkpoint(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = f"recovery-{uuid4().hex}@fitweek.test"
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
    repository = MySQLOrchestrationRepository(mysql_test_database.session_factory)
    environment = cli_environment(database_url=mysql_test_url, email=email)
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
                        "client_request_id": f"recovery-{uuid4().hex}",
                        **generation_payload(),
                    },
                )
                assert created.status_code == 202, created.text
                run_id = created.json()["run_id"]

                first_code, first_stdout, first_stderr = await asyncio.wait_for(
                    run_worker_once(environment, "recovery-predecessor"), timeout=30
                )
                assert first_code == 0, first_stderr
                assert first_stdout.strip() == "SUCCEEDED"
                steps_after_predecessor = await repository.list_steps(run_id)
                predecessor = next(
                    step for step in steps_after_predecessor if step.sequence_no == 1
                )
                successor = next(
                    step for step in steps_after_predecessor if step.sequence_no == 2
                )
                assert predecessor.status.value == "SUCCEEDED"
                assert successor.status.value == "READY"
                checkpoints_before = await repository.list_checkpoints(run_id)
                assert len(checkpoints_before) == 1
                predecessor_checkpoint = checkpoints_before[0]

                old_code, old_stdout, old_stderr = await asyncio.wait_for(
                    run_claim_then_exit(environment, "recovery-crashed-child"),
                    timeout=20,
                )
                assert old_code == 0, old_stderr
                old_claim = json.loads(old_stdout)
                assert old_claim["run_id"] == run_id
                assert old_claim["step_id"] == str(successor.id)
                old_step = next(
                    step
                    for step in await repository.list_steps(run_id)
                    if str(step.id) == old_claim["step_id"]
                )
                old_run = await repository.get_run(run_id)
                assert old_run is not None
                old_claimed_step = ClaimedStep(run=old_run, step=old_step)
                assert old_step.lease_token is not None
                assert old_step.status.value == "RUNNING"
                assert old_step.fencing_token == old_claim["fencing_token"] > 0

                async with mysql_test_database.session_factory() as session:
                    async with session.begin():
                        await session.execute(
                            update(AgentStepModel)
                            .where(AgentStepModel.id == str(successor.id))
                            .values(
                                lease_expires_at=datetime.now(UTC)
                                - timedelta(seconds=1)
                            )
                        )

                reap_code, _reap_stdout, reap_stderr = await asyncio.wait_for(
                    run_reaper_once(environment), timeout=30
                )
                assert reap_code == 0, reap_stderr
                reaped = next(
                    step
                    for step in await repository.list_steps(run_id)
                    if step.id == successor.id
                )
                assert reaped.status.value == "RETRY_SCHEDULED"
                assert reaped.worker_id is None
                assert reaped.lease_token is None
                assert reaped.fencing_token == old_step.fencing_token
                assert reaped.attempt_count == old_step.attempt_count

                with pytest.raises(StepLeaseLost):
                    await repository.heartbeat(
                        step_id=old_step.id,
                        worker_id="recovery-crashed-child",
                        lease_token=old_claimed_step.lease_token,
                        fencing_token=old_claimed_step.fencing_token,
                        lease_duration=timedelta(seconds=10),
                        now=datetime.now(UTC),
                    )
                with pytest.raises(StepLeaseLost):
                    await repository.complete_step(
                        claim=old_claimed_step,
                        handler_version="stale-test",
                        output_payload={"stale": True},
                        result_reference=None,
                        next_step_type=None,
                        run_status_after=PlanningRunStatus.COMPLETED,
                        now=datetime.now(UTC),
                    )
                with pytest.raises(StepLeaseLost):
                    await repository.fail_step(
                        claim=old_claimed_step,
                        error_code="STALE",
                        error_message="stale claim must not write",
                        retry_at=None,
                        now=datetime.now(UTC),
                    )

                recover_code, recover_stdout, recover_stderr = await asyncio.wait_for(
                    run_worker_once(environment, "recovery-new-worker"), timeout=45
                )
                assert recover_code == 0, recover_stderr
                assert recover_stdout.strip() == "SUCCEEDED", [
                    (step.status.value, step.last_error_code)
                    for step in await repository.list_steps(run_id)
                    if step.id == successor.id
                ]
                recovered = next(
                    step
                    for step in await repository.list_steps(run_id)
                    if step.id == successor.id
                )
                assert recovered.status.value == "SUCCEEDED"
                assert recovered.fencing_token > old_step.fencing_token
                assert recovered.attempt_count == old_step.attempt_count + 1
                checkpoints_after = await repository.list_checkpoints(run_id)
                assert checkpoints_after[0] == predecessor_checkpoint
                assert (
                    len(
                        [
                            item
                            for item in checkpoints_after
                            if item.step_id == predecessor.id
                        ]
                    )
                    == 1
                )
                assert len(checkpoints_after) == 2

                (
                    second_reap_code,
                    _second_reap_stdout,
                    second_reap_stderr,
                ) = await asyncio.wait_for(run_reaper_once(environment), timeout=30)
                assert second_reap_code == 0, second_reap_stderr
                assert len(await repository.list_steps(run_id)) == 3
                assert await repository.list_checkpoints(run_id) == checkpoints_after
    finally:
        await _delete_isolated_user(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_minimal_workflow_completes_across_api_and_worker_process_restarts(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    email = f"restart-workflow-{uuid4().hex}@fitweek.test"
    environment = cli_environment(database_url=mysql_test_url, email=email)
    active_apis = []
    api_pids: list[int] = []
    user_id = ""
    foreign_user_id = ""
    try:
        api_a = await start_api_process(environment)
        active_apis.append(api_a)
        api_pids.append(api_a.process.pid)
        async with AsyncClient(base_url=api_a.base_url) as client:
            user_id = (await client.get("/api/v1/users/me")).json()["id"]
            assert (
                await client.put("/api/v1/profiles/me", json=profile_payload())
            ).status_code == 200
            created = await client.post(
                "/api/v1/planning-runs",
                json={
                    "client_request_id": f"restart-workflow-{uuid4().hex}",
                    **generation_payload(),
                },
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
            assert [
                item["status"]
                for item in (
                    await client.get(f"/api/v1/planning-runs/{run_id}/steps")
                ).json()
            ] == ["READY"]
        await stop_api_process(active_apis.pop())

        foreign_user = replace(
            make_user(), email=f"restart-foreign-{uuid4().hex}@fitweek.test"
        )
        foreign_user_id = str(foreign_user.id)
        await MySQLUserAccountRepository(mysql_test_database.session_factory).save(
            foreign_user
        )
        foreign_repository = MySQLOrchestrationRepository(
            mysql_test_database.session_factory
        )
        foreign_run = make_run(
            user_id=foreign_user.id,
            client_request_id=f"restart-foreign-{uuid4().hex}",
        )
        foreign_now = datetime.now(UTC)
        await foreign_repository.create_run_with_initial_steps(
            foreign_run,
            (
                replace(
                    make_step(foreign_run.id),
                    priority=100,
                    next_execute_at=foreign_now,
                    created_at=foreign_now,
                    updated_at=foreign_now,
                ),
            ),
        )

        worker_outcomes = []
        for worker_id in (
            "restart-load",
            "restart-generate",
            "restart-safety",
            "restart-wait",
        ):
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr
            worker_outcomes.append(stdout.strip())
        assert worker_outcomes == [
            "SUCCEEDED",
            "SUCCEEDED",
            "SUCCEEDED",
            "WAITING_USER",
        ]

        api_b = await start_api_process(environment)
        active_apis.append(api_b)
        api_pids.append(api_b.process.pid)
        async with AsyncClient(base_url=api_b.base_url) as client:
            waiting = await client.get(f"/api/v1/planning-runs/{run_id}")
            assert waiting.status_code == 200
            assert waiting.json()["status"] == "WAITING_CONFIRMATION"
            plan_id = waiting.json()["result_reference"]
            plan = await client.get(f"/api/v1/plans/{plan_id}")
            expected_plan_version = plan.json()["version"]
            waiting_steps = (
                await client.get(f"/api/v1/planning-runs/{run_id}/steps")
            ).json()
            waiting_checkpoints = (
                await client.get(f"/api/v1/planning-runs/{run_id}/checkpoints")
            ).json()
            assert len(waiting_steps) == 4
            assert len(waiting_checkpoints) == 3
        await stop_api_process(active_apis.pop())

        api_c = await start_api_process(environment)
        active_apis.append(api_c)
        api_pids.append(api_c.process.pid)
        async with AsyncClient(base_url=api_c.base_url) as client:
            confirm_url = f"/api/v1/planning-runs/{run_id}/confirm"
            payload = {"expected_plan_version": expected_plan_version}
            first_confirm = await client.post(confirm_url, json=payload)
            retry_confirm = await client.post(confirm_url, json=payload)
            assert first_confirm.status_code == retry_confirm.status_code == 200
            resumed_steps = (
                await client.get(f"/api/v1/planning-runs/{run_id}/steps")
            ).json()
            assert len(resumed_steps) == 5
        await stop_api_process(active_apis.pop())

        final_code, final_stdout, final_stderr = await run_worker_once(
            environment, "restart-finalize"
        )
        assert final_code == 0, final_stderr
        assert final_stdout.strip() == "SUCCEEDED"

        api_d = await start_api_process(environment)
        active_apis.append(api_d)
        api_pids.append(api_d.process.pid)
        async with AsyncClient(base_url=api_d.base_url) as client:
            completed = await client.get(f"/api/v1/planning-runs/{run_id}")
            steps = await client.get(f"/api/v1/planning-runs/{run_id}/steps")
            checkpoints = await client.get(
                f"/api/v1/planning-runs/{run_id}/checkpoints"
            )
            audit = await client.get(f"/api/v1/planning-runs/{run_id}/audit")
            plan = await client.get(f"/api/v1/plans/{plan_id}")
            assert completed.json()["status"] == "COMPLETED"
            assert len(steps.json()) == len(checkpoints.json()) == 5
            assert plan.json()["status"] == "CONFIRMED"
            assert steps.json()[:3] == waiting_steps[:3]
            assert checkpoints.json()[:3] == waiting_checkpoints
            assert [item["sequence_no"] for item in audit.json()] == list(
                range(1, len(audit.json()) + 1)
            )
            assert (
                await client.get(f"/api/v1/planning-runs/{run_id}/audit")
            ).json() == audit.json()
        await stop_api_process(active_apis.pop())
        assert len(set(api_pids)) == 4
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        await _delete_isolated_user(mysql_test_database, user_id)
        await _delete_isolated_user(mysql_test_database, foreign_user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_waiting_user_confirm_and_finalize_survive_api_restart(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    email = f"restart-confirm-{uuid4().hex}@fitweek.test"
    environment = cli_environment(database_url=mysql_test_url, email=email)
    active_apis = []
    api_pids: list[int] = []
    user_id = ""
    try:
        api_a = await start_api_process(environment)
        active_apis.append(api_a)
        api_pids.append(api_a.process.pid)
        async with AsyncClient(base_url=api_a.base_url) as client:
            user_id = (await client.get("/api/v1/users/me")).json()["id"]
            assert (
                await client.put("/api/v1/profiles/me", json=profile_payload())
            ).status_code == 200
            created = await client.post(
                "/api/v1/planning-runs",
                json={
                    "client_request_id": f"restart-confirm-{uuid4().hex}",
                    **generation_payload(),
                },
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
        await stop_api_process(active_apis.pop())

        for worker_id in ("confirm-load", "confirm-generate", "confirm-safety"):
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr
            assert stdout.strip() == "SUCCEEDED"
        wait_code, wait_stdout, wait_stderr = await run_worker_once(
            environment, "confirm-wait"
        )
        assert wait_code == 0, wait_stderr
        assert wait_stdout.strip() == "WAITING_USER"

        api_b = await start_api_process(environment)
        active_apis.append(api_b)
        api_pids.append(api_b.process.pid)
        async with AsyncClient(base_url=api_b.base_url) as client:
            waiting = await client.get(f"/api/v1/planning-runs/{run_id}")
            assert waiting.json()["status"] == "WAITING_CONFIRMATION"
            plan_id = waiting.json()["result_reference"]
            plan = await client.get(f"/api/v1/plans/{plan_id}")
            expected_plan_version = plan.json()["version"]
            predecessor_checkpoints = (
                await client.get(f"/api/v1/planning-runs/{run_id}/checkpoints")
            ).json()
            predecessor_steps = (
                await client.get(f"/api/v1/planning-runs/{run_id}/steps")
            ).json()
            waiting_audit = (
                await client.get(f"/api/v1/planning-runs/{run_id}/audit")
            ).json()
            assert len(predecessor_checkpoints) == 3
        await stop_api_process(active_apis.pop())

        api_c = await start_api_process(environment)
        active_apis.append(api_c)
        api_pids.append(api_c.process.pid)
        async with AsyncClient(base_url=api_c.base_url) as client:
            confirm_url = f"/api/v1/planning-runs/{run_id}/confirm"
            payload = {"expected_plan_version": expected_plan_version}
            first = await client.post(confirm_url, json=payload)
            retried = await client.post(confirm_url, json=payload)
            assert first.status_code == retried.status_code == 200
            resumed_steps = (
                await client.get(f"/api/v1/planning-runs/{run_id}/steps")
            ).json()
            assert len(resumed_steps) == 5
            assert (
                sum(item["step_type"] == "FINALIZE_RUN" for item in resumed_steps) == 1
            )
        await stop_api_process(active_apis.pop())

        code, stdout, stderr = await run_worker_once(environment, "confirm-finalize")
        assert code == 0, stderr
        assert stdout.strip() == "SUCCEEDED"

        api_d = await start_api_process(environment)
        active_apis.append(api_d)
        api_pids.append(api_d.process.pid)
        async with AsyncClient(base_url=api_d.base_url) as client:
            completed = await client.get(f"/api/v1/planning-runs/{run_id}")
            steps = await client.get(f"/api/v1/planning-runs/{run_id}/steps")
            checkpoints = await client.get(
                f"/api/v1/planning-runs/{run_id}/checkpoints"
            )
            audit = await client.get(f"/api/v1/planning-runs/{run_id}/audit")
            plan = await client.get(f"/api/v1/plans/{plan_id}")
            assert completed.json()["status"] == "COMPLETED"
            assert len(steps.json()) == len(checkpoints.json()) == 5
            assert plan.json()["status"] == "CONFIRMED"
            assert steps.json()[:3] == predecessor_steps[:3]
            assert checkpoints.json()[:3] == predecessor_checkpoints
            assert audit.json()[: len(waiting_audit)] == waiting_audit
            assert (
                sum(item["step_type"] == "FINALIZE_RUN" for item in steps.json()) == 1
            )
            assert [item["sequence_no"] for item in audit.json()] == list(
                range(1, len(audit.json()) + 1)
            )
            assert (
                await client.get(f"/api/v1/planning-runs/{run_id}/audit")
            ).json() == audit.json()
        await stop_api_process(active_apis.pop())
        assert len(set(api_pids)) == 4
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        await _delete_isolated_user(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_waiting_user_cancel_survives_api_restart_without_finalize(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    email = f"restart-cancel-{uuid4().hex}@fitweek.test"
    environment = cli_environment(database_url=mysql_test_url, email=email)
    active_apis = []
    api_pids: list[int] = []
    user_id = ""
    try:
        api_a = await start_api_process(environment)
        active_apis.append(api_a)
        api_pids.append(api_a.process.pid)
        async with AsyncClient(base_url=api_a.base_url) as client:
            user_id = (await client.get("/api/v1/users/me")).json()["id"]
            assert (
                await client.put("/api/v1/profiles/me", json=profile_payload())
            ).status_code == 200
            created = await client.post(
                "/api/v1/planning-runs",
                json={
                    "client_request_id": f"restart-cancel-{uuid4().hex}",
                    **generation_payload(),
                },
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
        await stop_api_process(active_apis.pop())

        for worker_id in ("cancel-load", "cancel-generate", "cancel-safety"):
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr
            assert stdout.strip() == "SUCCEEDED"
        wait_code, wait_stdout, wait_stderr = await run_worker_once(
            environment, "cancel-wait"
        )
        assert wait_code == 0, wait_stderr
        assert wait_stdout.strip() == "WAITING_USER"

        api_b = await start_api_process(environment)
        active_apis.append(api_b)
        api_pids.append(api_b.process.pid)
        async with AsyncClient(base_url=api_b.base_url) as client:
            waiting = await client.get(f"/api/v1/planning-runs/{run_id}")
            assert waiting.json()["status"] == "WAITING_CONFIRMATION"
            plan_id = waiting.json()["result_reference"]
            waiting_steps = (
                await client.get(f"/api/v1/planning-runs/{run_id}/steps")
            ).json()
            predecessor_checkpoints = (
                await client.get(f"/api/v1/planning-runs/{run_id}/checkpoints")
            ).json()
            waiting_audit = (
                await client.get(f"/api/v1/planning-runs/{run_id}/audit")
            ).json()
            assert len(predecessor_checkpoints) == 3
        await stop_api_process(active_apis.pop())

        api_c = await start_api_process(environment)
        active_apis.append(api_c)
        api_pids.append(api_c.process.pid)
        async with AsyncClient(base_url=api_c.base_url) as client:
            cancel_url = f"/api/v1/planning-runs/{run_id}/cancel"
            cancelled = await client.post(cancel_url)
            repeated_cancel = await client.post(cancel_url)
            assert cancelled.status_code == repeated_cancel.status_code == 200
            assert (
                cancelled.json()["status"]
                == repeated_cancel.json()["status"]
                == "CANCELLED"
            )
        await stop_api_process(active_apis.pop())

        code, stdout, stderr = await run_worker_once(environment, "cancel-after")
        assert code == 0, stderr
        assert stdout.strip() == "EMPTY"

        api_d = await start_api_process(environment)
        active_apis.append(api_d)
        api_pids.append(api_d.process.pid)
        async with AsyncClient(base_url=api_d.base_url) as client:
            cancelled = await client.get(f"/api/v1/planning-runs/{run_id}")
            steps = await client.get(f"/api/v1/planning-runs/{run_id}/steps")
            checkpoints = await client.get(
                f"/api/v1/planning-runs/{run_id}/checkpoints"
            )
            plan = await client.get(f"/api/v1/plans/{plan_id}")
            audit = await client.get(f"/api/v1/planning-runs/{run_id}/audit")
            assert cancelled.json()["status"] == "CANCELLED"
            assert len(steps.json()) == 4
            assert len(checkpoints.json()) == 3
            assert steps.json()[:3] == waiting_steps[:3]
            assert steps.json()[3]["status"] == "CANCELLED"
            assert checkpoints.json() == predecessor_checkpoints
            assert audit.json()[: len(waiting_audit)] == waiting_audit
            assert all(item["step_type"] != "FINALIZE_RUN" for item in steps.json())
            assert plan.json()["status"] != "CONFIRMED"
            assert [item["sequence_no"] for item in audit.json()] == list(
                range(1, len(audit.json()) + 1)
            )
            assert (
                await client.get(f"/api/v1/planning-runs/{run_id}/audit")
            ).json() == audit.json()
        await stop_api_process(active_apis.pop())
        assert len(set(api_pids)) == 4
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        await _delete_isolated_user(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_worker_processes_claim_one_step_once(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    email = f"race-worker-{uuid4().hex}@fitweek.test"
    environment = cli_environment(database_url=mysql_test_url, email=email)
    active_apis = []
    worker_processes = []
    user_id = ""
    try:
        api_a = await start_api_process(environment)
        active_apis.append(api_a)
        async with AsyncClient(base_url=api_a.base_url) as client:
            user_id = (await client.get("/api/v1/users/me")).json()["id"]
            assert (
                await client.put("/api/v1/profiles/me", json=profile_payload())
            ).status_code == 200
            created = await client.post(
                "/api/v1/planning-runs",
                json={
                    "client_request_id": f"race-worker-{uuid4().hex}",
                    **generation_payload(),
                },
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
        await stop_api_process(active_apis.pop())

        for worker_id, outcome in (
            ("race-load", "SUCCEEDED"),
            ("race-generate", "SUCCEEDED"),
            ("race-safety", "SUCCEEDED"),
            ("race-wait", "WAITING_USER"),
        ):
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr
            assert stdout.strip() == outcome

        api_b = await start_api_process(environment)
        active_apis.append(api_b)
        async with AsyncClient(base_url=api_b.base_url) as client:
            waiting = await client.get(f"/api/v1/planning-runs/{run_id}")
            assert waiting.json()["status"] == "WAITING_CONFIRMATION"
            plan_id = waiting.json()["result_reference"]
            plan = await client.get(f"/api/v1/plans/{plan_id}")
            confirmed = await client.post(
                f"/api/v1/planning-runs/{run_id}/confirm",
                json={"expected_plan_version": plan.json()["version"]},
            )
            assert confirmed.status_code == 200
            ready_steps = (
                await client.get(f"/api/v1/planning-runs/{run_id}/steps")
            ).json()
            assert sum(item["step_type"] == "FINALIZE_RUN" for item in ready_steps) == 1
        await stop_api_process(active_apis.pop())

        worker_processes = list(
            await asyncio.gather(
                start_worker_once_process(environment, "race-worker-one"),
                start_worker_once_process(environment, "race-worker-two"),
            )
        )
        results = await asyncio.gather(
            *(wait_for_worker_process(process) for process in worker_processes)
        )
        assert all(code == 0 for code, _stdout, _stderr in results), [
            stderr for _code, _stdout, stderr in results
        ]
        assert sorted(stdout.strip() for _code, stdout, _stderr in results) == [
            "EMPTY",
            "SUCCEEDED",
        ]
        worker_processes.clear()

        api_c = await start_api_process(environment)
        active_apis.append(api_c)
        async with AsyncClient(base_url=api_c.base_url) as client:
            steps = (await client.get(f"/api/v1/planning-runs/{run_id}/steps")).json()
            checkpoints = (
                await client.get(f"/api/v1/planning-runs/{run_id}/checkpoints")
            ).json()
            audit = (await client.get(f"/api/v1/planning-runs/{run_id}/audit")).json()
            assert [item["status"] for item in steps] == ["SUCCEEDED"] * 5
            assert steps[-1]["attempt_count"] == 1
            assert len(checkpoints) == 5
            assert checkpoints[-1]["step_id"] == steps[-1]["id"]
            assert (
                sum(
                    item["event_type"] == "STEP_CLAIMED"
                    and item["step_id"] == steps[-1]["id"]
                    for item in audit
                )
                == 1
            )
            assert (
                sum(
                    item["event_type"] == "STEP_SUCCEEDED"
                    and item["step_id"] == steps[-1]["id"]
                    for item in audit
                )
                == 1
            )
            plan = await client.get(f"/api/v1/plans/{plan_id}")
            assert plan.json()["status"] == "CONFIRMED"
        await stop_api_process(active_apis.pop())

        code, stdout, stderr = await run_worker_once(environment, "race-successor")
        assert code == 0, stderr
        assert stdout.strip() == "EMPTY"
    finally:
        while worker_processes:
            await stop_worker_process_for_cleanup(worker_processes.pop())
        while active_apis:
            await stop_api_process(active_apis.pop())
        await _delete_isolated_user(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_loop_signal_shutdown_releases_process_and_resources(
    mysql_test_url: str,
) -> None:
    environment = cli_environment(
        database_url=mysql_test_url,
        email=f"signal-loop-{uuid4().hex}@fitweek.test",
    )
    environment["ORCHESTRATOR_POLL_INTERVAL_SECONDS"] = "0.05"
    loop_processes = []
    try:
        for worker_id in ("signal-loop-one", "signal-loop-two"):
            loop_process = await start_worker_loop_process(environment, worker_id)
            loop_processes.append(loop_process)
            await wait_for_worker_loop_start(loop_process)
            code, stdout, stderr = await signal_worker_loop_gracefully(loop_process)
            assert code == 0, stderr
            assert "traceback" not in (stdout + stderr).casefold()
            loop_processes.pop()
        code, stdout, stderr = await run_worker_once(environment, "signal-fresh")
        assert code == 0, stderr
        assert stdout.strip() == "EMPTY"
    finally:
        while loop_processes:
            await stop_worker_process_for_cleanup(loop_processes.pop())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redis_unavailable_falls_back_to_mysql_polling(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    email = f"redis-poll-{uuid4().hex}@fitweek.test"
    environment = cli_environment(database_url=mysql_test_url, email=email)
    unreachable_port = reserve_unreachable_loopback_port()
    environment.update(
        {
            "REDIS_ENABLED": "true",
            "REDIS_URL": f"redis://127.0.0.1:{unreachable_port}/0",
            "REDIS_CONNECT_TIMEOUT_SECONDS": "0.1",
            "REDIS_SOCKET_TIMEOUT_SECONDS": "0.1",
        }
    )
    active_apis = []
    user_id = ""
    try:
        api_a = await start_api_process(environment)
        active_apis.append(api_a)
        async with AsyncClient(base_url=api_a.base_url) as client:
            readiness = await client.get("/health/ready")
            assert readiness.status_code == 200
            assert readiness.json()["checks"]["redis"] == "unavailable"
            user_id = (await client.get("/api/v1/users/me")).json()["id"]
            assert (
                await client.put("/api/v1/profiles/me", json=profile_payload())
            ).status_code == 200
            created = await client.post(
                "/api/v1/planning-runs",
                json={
                    "client_request_id": f"redis-poll-{uuid4().hex}",
                    **generation_payload(),
                },
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
        await stop_api_process(active_apis.pop())

        code, stdout, stderr = await run_worker_once(environment, "redis-poll")
        assert code == 0, stderr
        assert stdout.strip() == "SUCCEEDED"

        api_b = await start_api_process(environment)
        active_apis.append(api_b)
        async with AsyncClient(base_url=api_b.base_url) as client:
            steps = (await client.get(f"/api/v1/planning-runs/{run_id}/steps")).json()
            checkpoints = (
                await client.get(f"/api/v1/planning-runs/{run_id}/checkpoints")
            ).json()
            audit = (await client.get(f"/api/v1/planning-runs/{run_id}/audit")).json()
            assert readiness.json()["status"] == "degraded"
            assert [item["status"] for item in steps] == ["SUCCEEDED", "READY"]
            assert len(checkpoints) == 1
            assert sum(item["event_type"] == "STEP_SUCCEEDED" for item in audit) == 1
        await stop_api_process(active_apis.pop())
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        await _delete_isolated_user(mysql_test_database, user_id)
