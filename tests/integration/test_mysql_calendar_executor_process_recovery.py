"""Red-first MySQL Calendar executor CLI composition contracts."""

from __future__ import annotations

import asyncio
import socket
import sys
from argparse import Namespace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import AsyncClient, HTTPError
from pydantic import SecretStr
from sqlalchemy import delete, func, select

import app.api.dependencies as dependencies_module
import app.orchestration.cli as cli_module
import app.orchestration.mysql_runtime as mysql_runtime_module
from app.application.calendar_operation_orchestration import CalendarOperationRunService
from app.application.calendar_operations import CalendarOperationService
from app.config import PersistenceBackend, Settings
from app.domain.orchestration.enums import StepType
from app.orchestration.clock import SystemClock
from app.orchestration.mysql_runtime import build_mysql_cli_runtime
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AgentStepModel,
    AuditEventModel,
    CalendarEventBindingModel,
    CalendarOperationAttemptModel,
    CheckpointModel,
    PlanningRunModel,
    StepDependencyModel,
)
from app.persistence.mysql.orchestration_repository import MySQLOrchestrationRepository
from app.persistence.mysql.plan_repository import MySQLPlanRepository
from tests.integration.test_mysql_calendar_draft_review_runtime import (
    _cleanup,
    _confirmed_plan,
    _draft_payload,
    _mysql_api_environment,
)
from tests.support.mysql_orchestrator_process import (
    ApiProcess,
    cli_environment,
    run_calendar_executor_crash,
    run_reaper_once,
    run_worker_once,
    start_api_process,
    stop_api_process,
)


def _mysql_executor_settings(database_url: str) -> Settings:
    return Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(database_url),
        redis_enabled=False,
        model_gateway_enabled=False,
        orchestrator_enabled=True,
        single_user_email=f"calendar-executor-{uuid4().hex}@fitweek.test",
        calendar_write_enabled=True,
        calendar_write_provider="scripted",
        _env_file=None,
    )


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _start_calendar_stub() -> ApiProcess:
    port = _reserve_loopback_port()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        "tests.stub_calendar_server.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stub = ApiProcess(process=process, port=port)
    try:
        async with AsyncClient(base_url=stub.base_url) as client:
            for _ in range(100):
                if process.returncode is not None:
                    raise RuntimeError("owned Calendar Stub exited before readiness")
                try:
                    response = await client.get("/admin/events", timeout=0.25)
                    if response.status_code == 200:
                        return stub
                except HTTPError:
                    pass
                await asyncio.sleep(0.05)
    except BaseException:
        await stop_api_process(stub)
        raise
    await stop_api_process(stub)
    raise RuntimeError("owned Calendar Stub did not become ready")


def _calendar_cli_environment(
    database_url: str, email: str, stub_port: int
) -> dict[str, str]:
    environment = cli_environment(database_url=database_url, email=email)
    environment.update(
        {
            "CALENDAR_WRITE_ENABLED": "true",
            "CALENDAR_WRITE_PROVIDER": "http",
            "CALENDAR_WRITE_BASE_URL": f"http://127.0.0.1:{stub_port}",
            "CALENDAR_WRITE_API_KEY": "local-calendar-write-test-key",
        }
    )
    return environment


async def _create_approved_calendar_run(
    client: AsyncClient, token: str
) -> tuple[dict[str, object], dict[str, object]]:
    plan = await _confirmed_plan(client, token)
    root = str(plan["root_plan_id"] or plan["id"])
    created = await client.post(
        f"/api/v1/plans/{root}/revisions/{plan['revision']}/calendar-operation-drafts",
        json=_draft_payload(token, plan),
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    approved = await client.post(
        f"/api/v1/calendar-operation-drafts/{draft['id']}/approve",
        json={"expected_version": draft["version"]},
    )
    assert approved.status_code == 200, approved.text
    run = await client.post(
        "/api/v1/calendar-operation-runs",
        json={"client_request_id": f"run-{token}", "draft_id": draft["id"]},
    )
    assert run.status_code == 202, run.text
    return approved.json(), run.json()


async def _cleanup_executor(database: Database, user_id: str | None) -> None:
    """Delete only the generated user's orchestration facts before Draft cleanup."""

    if user_id is None:
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
                delete(AuditEventModel).where(AuditEventModel.run_id.in_(run_ids))
            )
            await session.execute(
                delete(CheckpointModel).where(CheckpointModel.run_id.in_(run_ids))
            )
            await session.execute(
                delete(StepDependencyModel).where(
                    StepDependencyModel.step_id.in_(step_ids)
                    | StepDependencyModel.dependency_step_id.in_(step_ids)
                )
            )
            await session.execute(
                delete(AgentStepModel).where(AgentStepModel.run_id.in_(run_ids))
            )
            await session.execute(
                delete(PlanningRunModel).where(PlanningRunModel.id.in_(run_ids))
            )
    await _cleanup(database, __import__("uuid").UUID(user_id))


async def _attempt_binding_snapshot(
    database: Database,
    *,
    draft_id: str,
    user_id: str,
) -> tuple[tuple[tuple[object, ...], ...], tuple[tuple[object, ...], ...]]:
    """Read every durable Attempt and Binding fact without altering it."""

    async with database.session_factory() as session:
        attempts = (
            await session.scalars(
                select(CalendarOperationAttemptModel)
                .where(CalendarOperationAttemptModel.draft_id == draft_id)
                .order_by(
                    CalendarOperationAttemptModel.item_id,
                    CalendarOperationAttemptModel.attempt_no,
                )
            )
        ).all()
        bindings = (
            await session.scalars(
                select(CalendarEventBindingModel)
                .where(CalendarEventBindingModel.user_id == user_id)
                .order_by(CalendarEventBindingModel.id)
            )
        ).all()
    return (
        tuple(
            (
                row.id,
                row.user_id,
                row.draft_id,
                row.item_id,
                row.attempt_no,
                row.outcome,
                row.error_code,
                row.response_reference_hash,
                row.started_at,
                row.finished_at,
            )
            for row in attempts
        ),
        tuple(
            (
                row.id,
                row.user_id,
                row.provider,
                row.calendar_id,
                row.root_plan_id,
                row.session_id,
                row.external_event_id,
                row.stable_uid,
                row.last_payload_fingerprint,
                row.status,
                row.created_at,
                row.updated_at,
                row.version,
            )
            for row in bindings
        ),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_executor_cli_is_only_enabled_writer_and_closes_gateway(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The independent CLI, not the API, owns enabled Calendar execution."""

    api_services = dependencies_module.build_mysql_calendar_operation_services(
        _mysql_executor_settings(mysql_test_url),
        plans=MySQLPlanRepository(mysql_test_database.session_factory),
        sessions=mysql_test_database.session_factory,
        clock=SystemClock(),
    )
    try:
        assert api_services.gateway.provider_name == "none"
        assert api_services.gateway._enabled is False
    finally:
        await api_services.gateway.close()

    runtime, database = await build_mysql_cli_runtime(
        _mysql_executor_settings(mysql_test_url),
        worker_id="calendar-executor-composition",
    )
    try:
        assert isinstance(
            runtime.calendar_operation_run_service,
            CalendarOperationRunService,
        )
        assert isinstance(runtime.calendar_operation_service, CalendarOperationService)
        assert runtime.calendar_operation_gateway.provider_name == "scripted"
        for step_type in (
            StepType.LOAD_CALENDAR_OPERATION_DRAFT,
            StepType.VALIDATE_CALENDAR_OPERATION_APPROVAL,
            StepType.EXECUTE_CALENDAR_OPERATION_ITEMS,
            StepType.VERIFY_CALENDAR_OPERATION_RESULTS,
            StepType.FINALIZE_CALENDAR_OPERATION,
        ):
            runtime.registry.get(step_type)
    finally:
        if runtime.calendar_operation_gateway is not None:
            await runtime.calendar_operation_gateway.close()
        await database.dispose()

    close_calls = 0
    dispose_calls = 0

    class CloseSentinel:
        async def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    class WorkerSentinel:
        async def run_once(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(outcome="EMPTY")

    class DatabaseSentinel:
        async def dispose(self) -> None:
            nonlocal dispose_calls
            dispose_calls += 1

    normal_runtime = SimpleNamespace(
        worker=WorkerSentinel(),
        reaper=SimpleNamespace(run_once=lambda: None),
        calendar_operation_gateway=CloseSentinel(),
        schedule_calendar_gateway=None,
        schedule_model_gateway=None,
        session_design_model_gateway=None,
        profile_agent_model_gateway=None,
    )

    async def normal_builder(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return normal_runtime, DatabaseSentinel()

    def executor_settings() -> Settings:
        return _mysql_executor_settings(mysql_test_url)

    monkeypatch.setattr(cli_module, "get_settings", executor_settings)
    monkeypatch.setattr(cli_module, "build_mysql_cli_runtime", normal_builder)
    assert (
        await cli_module._run(
            Namespace(role="worker", once=True, loop=False, worker_id="close-sentinel")
        )
        == 0
    )
    assert close_calls == dispose_calls == 1

    async def failing_builder(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("SENTINEL_SECRET_MUST_NOT_APPEAR")

    monkeypatch.setattr(cli_module, "build_mysql_cli_runtime", failing_builder)
    assert (
        await asyncio.to_thread(
            cli_module.main, ["worker", "--once", "--worker-id", "redacted"]
        )
        == 2
    )
    stderr = capsys.readouterr().err
    assert "RuntimeError" in stderr
    assert "SENTINEL_SECRET_MUST_NOT_APPEAR" not in stderr

    construction_close_calls = 0
    construction_dispose_calls = 0

    class ConstructionGatewaySentinel:
        async def close(self) -> None:
            nonlocal construction_close_calls
            construction_close_calls += 1

    async def counted_dispose(self) -> None:  # type: ignore[no-untyped-def]
        nonlocal construction_dispose_calls
        construction_dispose_calls += 1

    def executor_factory(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(gateway=ConstructionGatewaySentinel(), service=None)

    def fail_after_calendar_resource(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("construction sentinel")

    monkeypatch.setattr(
        dependencies_module,
        "build_mysql_calendar_executor_services",
        executor_factory,
    )
    monkeypatch.setattr(
        mysql_runtime_module,
        "build_mysql_orchestration_runtime",
        fail_after_calendar_resource,
    )
    monkeypatch.setattr(Database, "dispose", counted_dispose)
    with pytest.raises(RuntimeError, match="construction sentinel"):
        await mysql_runtime_module.build_mysql_cli_runtime(
            _mysql_executor_settings(mysql_test_url),
            worker_id="construction-close-sentinel",
        )
    assert construction_close_calls == construction_dispose_calls == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_executor_survives_api_worker_restarts_and_response_loss(
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """API stays providerless while five independent CLI Workers finish one Run."""

    token = uuid4().hex
    email = f"calendar-executor-{token}@fitweek.test"
    api_environment = _mysql_api_environment(mysql_test_url, email)
    api_environment.update(
        {
            "ORCHESTRATOR_ENABLED": "true",
            "CALENDAR_WRITE_ENABLED": "true",
            "CALENDAR_WRITE_PROVIDER": "http",
        }
    )
    stub: ApiProcess | None = None
    api_a: ApiProcess | None = None
    api_b: ApiProcess | None = None
    user_id = None
    try:
        stub = await _start_calendar_stub()
        api_environment.update(
            {
                "CALENDAR_WRITE_BASE_URL": f"http://127.0.0.1:{stub.port}",
                "CALENDAR_WRITE_API_KEY": "local-calendar-write-test-key",
            }
        )
        api_a = await start_api_process(api_environment)
        async with AsyncClient(base_url=api_a.base_url, timeout=10) as client:
            user_id = (await client.get("/api/v1/users/me")).json()["id"]
            assert user_id is not None
            approved, run = await _create_approved_calendar_run(client, token)
            direct = await client.post(
                f"/api/v1/calendar-operation-drafts/{approved['id']}/execute"
            )
            assert direct.status_code == 409, direct.text
            retry = await client.post(
                f"/api/v1/calendar-operation-drafts/{approved['id']}/retry"
            )
            assert retry.status_code == 409, retry.text
            assert (await client.get("/health/ready")).json()["checks"]["mysql"] == "ok"
        async with AsyncClient(base_url=stub.base_url, timeout=10) as client:
            baseline = (await client.get("/admin/events")).json()
            assert baseline["event_count"] == baseline["write_request_count"] == 0
        await stop_api_process(api_a)
        api_a = None

        cli_environment = _calendar_cli_environment(mysql_test_url, email, stub.port)
        async with AsyncClient(base_url=stub.base_url, timeout=10) as client:
            configured = await client.post(
                "/admin/fail-next",
                json={"status_code": 500, "after_commit": True, "count": 1},
            )
            assert configured.status_code == 200, configured.text
        for index in range(5):
            code, _stdout, stderr = await run_worker_once(
                cli_environment, f"calendar-executor-{index}"
            )
            assert code == 0, stderr

        api_b = await start_api_process(api_environment)
        async with AsyncClient(base_url=api_b.base_url, timeout=10) as client:
            current = await client.get(f"/api/v1/calendar-operation-runs/{run['id']}")
            assert current.status_code == 200, current.text
            run_snapshot = current.json()
            assert run_snapshot["status"] == "COMPLETED"
            steps = await client.get(
                f"/api/v1/calendar-operation-runs/{run['id']}/steps"
            )
            checkpoints = await client.get(
                f"/api/v1/calendar-operation-runs/{run['id']}/checkpoints"
            )
            step_snapshot = steps.json()
            checkpoint_snapshot = checkpoints.json()
            assert len(step_snapshot) == len(checkpoint_snapshot) == 5
            assert {step["status"] for step in step_snapshot} == {"SUCCEEDED"}
            draft = await client.get(
                f"/api/v1/calendar-operation-drafts/{approved['id']}"
            )
            draft_snapshot = draft.json()
            assert draft_snapshot["status"] == "SUCCEEDED"
            item_session_ids = {item["session_id"] for item in draft_snapshot["items"]}
        async with AsyncClient(base_url=stub.base_url, timeout=10) as client:
            events = (await client.get("/admin/events")).json()
            stub_snapshot = (
                events["event_count"],
                events["write_request_count"],
                events["duplicate_reuse_count"],
            )
            assert stub_snapshot == (2, 3, 1)
        attempt_snapshot, binding_snapshot = await _attempt_binding_snapshot(
            mysql_test_database,
            draft_id=str(approved["id"]),
            user_id=user_id,
        )
        assert len(attempt_snapshot) == 3
        assert all(row[0] and row[4] > 0 and row[5] for row in attempt_snapshot)
        assert len(binding_snapshot) == 2
        assert {row[9] for row in binding_snapshot} == {"ACTIVE"}
        assert all(row[8] for row in binding_snapshot)
        binding_session_ids = {row[5] for row in binding_snapshot}
        assert len(binding_session_ids) == 2
        assert binding_session_ids == item_session_ids
        code, stdout, stderr = await run_worker_once(cli_environment, "calendar-idle")
        assert code == 0 and "empty" in stdout.lower(), stderr
        async with AsyncClient(base_url=stub.base_url, timeout=10) as client:
            events = (await client.get("/admin/events")).json()
            assert (
                events["event_count"],
                events["write_request_count"],
                events["duplicate_reuse_count"],
            ) == stub_snapshot
        assert await _attempt_binding_snapshot(
            mysql_test_database,
            draft_id=str(approved["id"]),
            user_id=user_id,
        ) == (attempt_snapshot, binding_snapshot)
        async with AsyncClient(base_url=api_b.base_url, timeout=10) as client:
            assert (
                await client.get(f"/api/v1/calendar-operation-runs/{run['id']}")
            ).json() == run_snapshot
            assert (
                await client.get(f"/api/v1/calendar-operation-runs/{run['id']}/steps")
            ).json() == step_snapshot
            assert (
                await client.get(
                    f"/api/v1/calendar-operation-runs/{run['id']}/checkpoints"
                )
            ).json() == checkpoint_snapshot
            assert (
                await client.get(f"/api/v1/calendar-operation-drafts/{approved['id']}")
            ).json() == draft_snapshot
    finally:
        if api_a is not None:
            await stop_api_process(api_a)
        if api_b is not None:
            await stop_api_process(api_b)
        if stub is not None:
            await stop_api_process(stub)
        await _cleanup_executor(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_executor_recovers_commit_before_binding_and_partial_success_crashes(  # noqa: E501
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """Owned crash helpers recover durable EXECUTING work through Reaper fencing."""

    for mode, crash_code in (("post_commit_pre_binding", 73), ("post_first_item", 74)):
        token = uuid4().hex
        email = f"calendar-crash-{token}@fitweek.test"
        api_environment = _mysql_api_environment(mysql_test_url, email)
        api_environment.update({"ORCHESTRATOR_ENABLED": "true"})
        stub: ApiProcess | None = None
        api: ApiProcess | None = None
        user_id = None
        try:
            stub = await _start_calendar_stub()
            api_environment.update(
                {
                    "CALENDAR_WRITE_ENABLED": "true",
                    "CALENDAR_WRITE_PROVIDER": "http",
                    "CALENDAR_WRITE_BASE_URL": f"http://127.0.0.1:{stub.port}",
                    "CALENDAR_WRITE_API_KEY": "local-calendar-write-test-key",
                }
            )
            api = await start_api_process(api_environment)
            async with AsyncClient(base_url=api.base_url, timeout=10) as client:
                user_id = (await client.get("/api/v1/users/me")).json()["id"]
                assert user_id is not None
                approved, run = await _create_approved_calendar_run(client, token)
            await stop_api_process(api)
            api = None
            cli_env = _calendar_cli_environment(mysql_test_url, email, stub.port)
            cli_env["ORCHESTRATOR_LEASE_SECONDS"] = "1"
            for index in range(2):
                code, _stdout, stderr = await run_worker_once(
                    cli_env, f"calendar-before-crash-{mode}-{index}"
                )
                assert code == 0, stderr
            code, _stdout, stderr = await run_calendar_executor_crash(
                cli_env, mode, f"calendar-crash-{mode}"
            )
            assert code == crash_code, stderr
            api = await start_api_process(api_environment)
            async with AsyncClient(base_url=api.base_url, timeout=10) as client:
                interrupted = await client.get(
                    f"/api/v1/calendar-operation-drafts/{approved['id']}"
                )
                assert interrupted.json()["status"] == "EXECUTING"
                items = interrupted.json()["items"]
                assert len(items) == 2
                if mode == "post_commit_pre_binding":
                    assert {item["status"] for item in items} == {"RUNNING", "PENDING"}
                else:
                    assert [item["status"] for item in items].count("SUCCEEDED") == 1
                before = await client.get(
                    f"/api/v1/calendar-operation-runs/{run['id']}/steps"
                )
                assert any(step["status"] == "RUNNING" for step in before.json())
            repository = MySQLOrchestrationRepository(
                mysql_test_database.session_factory
            )
            run_uuid = __import__("uuid").UUID(run["id"])
            before_steps = await repository.list_steps(run_uuid)
            execute_before = next(
                step
                for step in before_steps
                if step.step_type is StepType.EXECUTE_CALENDAR_OPERATION_ITEMS
            )
            assert execute_before.status.value == "RUNNING"
            async with AsyncClient(base_url=stub.base_url, timeout=10) as client:
                boundary = (await client.get("/admin/events")).json()
                assert boundary["event_count"] == boundary["write_request_count"] == 1
            async with mysql_test_database.session_factory() as session:
                attempt_count = await session.scalar(
                    select(func.count())
                    .select_from(CalendarOperationAttemptModel)
                    .where(CalendarOperationAttemptModel.draft_id == approved["id"])
                )
                bindings = await session.scalar(
                    select(func.count())
                    .select_from(CalendarEventBindingModel)
                    .where(CalendarEventBindingModel.user_id == user_id)
                )
                assert attempt_count == 1
                assert bindings == 0 if mode == "post_commit_pre_binding" else 1
            await stop_api_process(api)
            api = None
            await asyncio.sleep(1.1)
            code, _stdout, stderr = await run_reaper_once(cli_env)
            assert code == 0, stderr
            for index in range(3):
                code, _stdout, stderr = await run_worker_once(
                    cli_env, f"calendar-after-crash-{mode}-{index}"
                )
                assert code == 0, stderr
            after_steps = await repository.list_steps(run_uuid)
            execute_after = next(
                step for step in after_steps if step.id == execute_before.id
            )
            assert execute_after.fencing_token > execute_before.fencing_token
            api = await start_api_process(api_environment)
            async with AsyncClient(base_url=api.base_url, timeout=10) as client:
                completed = await client.get(
                    f"/api/v1/calendar-operation-runs/{run['id']}"
                )
                run_snapshot = completed.json()
                assert run_snapshot["status"] == "COMPLETED"
                draft = await client.get(
                    f"/api/v1/calendar-operation-drafts/{approved['id']}"
                )
                draft_snapshot = draft.json()
                assert draft_snapshot["status"] == "SUCCEEDED"
                item_snapshot = draft_snapshot["items"]
                assert len(item_snapshot) == 2
                assert {item["status"] for item in item_snapshot} == {"SUCCEEDED"}
                item_session_ids = {item["session_id"] for item in item_snapshot}
                steps = await client.get(
                    f"/api/v1/calendar-operation-runs/{run['id']}/steps"
                )
                checkpoints = await client.get(
                    f"/api/v1/calendar-operation-runs/{run['id']}/checkpoints"
                )
                step_snapshot = steps.json()
                checkpoint_snapshot = checkpoints.json()
                assert len(step_snapshot) == len(checkpoint_snapshot) == 5
                assert {step["status"] for step in step_snapshot} == {"SUCCEEDED"}
            async with AsyncClient(base_url=stub.base_url, timeout=10) as client:
                events = (await client.get("/admin/events")).json()
                stub_snapshot = (
                    events["event_count"],
                    events["write_request_count"],
                    events["duplicate_reuse_count"],
                )
                assert stub_snapshot == (
                    (2, 3, 1) if mode == "post_commit_pre_binding" else (2, 2, 0)
                )
            attempt_snapshot, binding_snapshot = await _attempt_binding_snapshot(
                mysql_test_database,
                draft_id=str(approved["id"]),
                user_id=user_id,
            )
            assert len(attempt_snapshot) == (
                3 if mode == "post_commit_pre_binding" else 2
            )
            assert len(binding_snapshot) == 2
            assert {row[9] for row in binding_snapshot} == {"ACTIVE"}
            binding_session_ids = {row[5] for row in binding_snapshot}
            assert len(binding_session_ids) == 2
            assert binding_session_ids == item_session_ids
            async with mysql_test_database.session_factory() as session:
                completed_runs = await session.scalar(
                    select(func.count())
                    .select_from(PlanningRunModel)
                    .where(
                        PlanningRunModel.id == run["id"],
                        PlanningRunModel.status == "COMPLETED",
                    )
                )
                assert completed_runs == 1
            code, stdout, stderr = await run_worker_once(
                cli_env, f"calendar-idle-after-crash-{mode}"
            )
            assert code == 0 and "empty" in stdout.lower(), stderr
            async with AsyncClient(base_url=stub.base_url, timeout=10) as client:
                events = (await client.get("/admin/events")).json()
                assert (
                    events["event_count"],
                    events["write_request_count"],
                    events["duplicate_reuse_count"],
                ) == stub_snapshot
            assert await _attempt_binding_snapshot(
                mysql_test_database,
                draft_id=str(approved["id"]),
                user_id=user_id,
            ) == (attempt_snapshot, binding_snapshot)
            async with AsyncClient(base_url=api.base_url, timeout=10) as client:
                assert (
                    await client.get(f"/api/v1/calendar-operation-runs/{run['id']}")
                ).json() == run_snapshot
                assert (
                    await client.get(
                        f"/api/v1/calendar-operation-runs/{run['id']}/steps"
                    )
                ).json() == step_snapshot
                assert (
                    await client.get(
                        f"/api/v1/calendar-operation-runs/{run['id']}/checkpoints"
                    )
                ).json() == checkpoint_snapshot
                assert (
                    await client.get(
                        f"/api/v1/calendar-operation-drafts/{approved['id']}"
                    )
                ).json() == draft_snapshot
        finally:
            if api is not None:
                await stop_api_process(api)
            if stub is not None:
                await stop_api_process(stub)
            await _cleanup_executor(mysql_test_database, user_id)
