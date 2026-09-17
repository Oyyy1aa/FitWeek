"""Persistent SessionDesign application orchestration contracts."""

from __future__ import annotations

import asyncio
import json
import sys
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select

import app.main as main_module
from app.application.session_design_orchestration import (
    SessionDesignApplicationRunService,
)
from app.config import PersistenceBackend, Settings
from app.domain.orchestration.enums import StepType
from app.orchestration.mysql_runtime import build_mysql_cli_runtime
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AgentStepModel,
    AuditEventModel,
    CheckpointModel,
    PlanningRunModel,
    SessionDesignApplicationResultModel,
    SessionDesignDraftModel,
    StepDependencyModel,
)
from app.persistence.mysql.session_design_repository import MySQLSessionDesignRepository
from tests.integration.test_mysql_session_design_draft_runtime import (
    _cleanup,
    _generation_payload,
)
from tests.support.mysql_orchestrator_process import (
    cli_environment,
    run_reaper_once,
    run_worker_once,
    start_api_process,
    stop_api_process,
)


def _mysql_settings(
    database_url: str, email: str = "session-run@fitweek.test"
) -> Settings:
    return Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(database_url),
        redis_enabled=False,
        model_gateway_enabled=False,
        orchestrator_enabled=True,
        single_user_email=email,
        _env_file=None,
    )


async def _cleanup_session_orchestration(
    database: Database, user_id: UUID | None
) -> None:
    if user_id is None:
        return
    owner = str(user_id)
    async with database.session_factory() as session:
        async with session.begin():
            run_ids = (
                await session.scalars(
                    select(PlanningRunModel.id).where(PlanningRunModel.user_id == owner)
                )
            ).all()
            step_ids = (
                await session.scalars(
                    select(AgentStepModel.id).where(AgentStepModel.run_id.in_(run_ids))
                )
            ).all()
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id == owner)
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
    await _cleanup(database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_session_design_run_dependency_never_uses_memory_container(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MySQL composes the durable Session Run service and all six handlers."""

    settings = _mysql_settings(
        mysql_test_url, f"session-dependency-{uuid4().hex}@fitweek.test"
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    application = main_module.create_application()
    user_id: UUID | None = None
    try:
        async with application.router.lifespan_context(application):
            user_id = application.state.local_user.id
            assert application.state.business_container is None
            assert isinstance(
                application.state.session_design_repository,
                MySQLSessionDesignRepository,
            )
            assert isinstance(
                application.state.session_design_application_run_service,
                SessionDesignApplicationRunService,
            )

        runtime, database = await build_mysql_cli_runtime(
            settings, worker_id="session-run-dependency-cli"
        )
        try:
            assert isinstance(
                runtime.session_design_application_run_service,
                SessionDesignApplicationRunService,
            )
            for step_type in (
                StepType.LOAD_SESSION_APPLICATION_CONTEXT,
                StepType.VALIDATE_SESSION_DESIGN_TARGET,
                StepType.BUILD_SESSION_PLAN_REVISION,
                StepType.VERIFY_SESSION_PLAN_SAFETY,
                StepType.WAIT_FOR_PLAN_REVISION_CONFIRMATION,
                StepType.FINALIZE_SESSION_APPLICATION,
            ):
                runtime.registry.get(step_type)
        finally:
            if runtime.session_design_model_gateway is not None:
                await runtime.session_design_model_gateway.close()
            if runtime.profile_agent_model_gateway is not None:
                await runtime.profile_agent_model_gateway.close()
            await database.dispose()
    finally:
        await _cleanup_session_orchestration(mysql_test_database, user_id)


async def _setup_accepted_session_design(
    client: AsyncClient, token: str
) -> dict[str, object]:
    user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
    profile = await client.put(
        "/api/v1/profiles/me",
        json={
            "experience_level": "BEGINNER",
            "weekly_frequency": 2,
            "max_session_minutes": 45,
            "primary_goal": "GENERAL_FITNESS",
            "scope_confirmed": True,
        },
    )
    assert profile.status_code == 200, profile.text
    generated = await client.post(
        "/api/v1/plans/generate", json=_generation_payload(token)
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed = await client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    source = confirmed.json()
    target = source["sessions"][0]
    created = await client.post(
        "/api/v1/session-designs",
        json={
            "client_request_id": f"session-design-{token}",
            "target_date": target["scheduled_start"][:10],
            "target_duration_minutes": target["estimated_minutes"],
            "location": target["location_type"],
            "goal": "GENERAL_FITNESS",
            "preferred_session_type": target["session_type"],
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    accepted = await client.post(
        f"/api/v1/session-designs/{draft['id']}/accept",
        json={"expected_version": draft["version"]},
    )
    assert accepted.status_code == 200, accepted.text
    return {
        "user_id": user_id,
        "draft": accepted.json(),
        "plan": source,
        "target": target,
    }


def _run_payload(setup: dict[str, object], token: str) -> dict[str, object]:
    draft = setup["draft"]
    plan = setup["plan"]
    target = setup["target"]
    assert (
        isinstance(draft, dict) and isinstance(plan, dict) and isinstance(target, dict)
    )
    return {
        "client_request_id": f"session-run-{token}",
        "draft_id": draft["id"],
        "root_plan_id": plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "target_session_id": target["id"],
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_design_run_survives_api_worker_restarts_and_confirmation(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    token = uuid4().hex
    email = f"session-run-{token}@fitweek.test"
    environment = cli_environment(database_url=mysql_test_url, email=email)
    environment["APP_ENV"] = "test"
    environment["MODEL_GATEWAY_ENABLED"] = "false"
    active_apis = []
    user_id: UUID | None = None
    try:
        api_a = await start_api_process(environment)
        active_apis.append(api_a)
        async with AsyncClient(base_url=api_a.base_url) as client:
            setup = await _setup_accepted_session_design(client, token)
            user_id = setup["user_id"]
            payload = _run_payload(setup, token)
            created = await client.post(
                "/api/v1/session-design-application-runs", json=payload
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["id"]
            repeated = await client.post(
                "/api/v1/session-design-application-runs", json=payload
            )
            assert repeated.status_code == 200
            assert repeated.json()["id"] == run_id
            conflict = await client.post(
                "/api/v1/session-design-application-runs",
                json={**payload, "target_session_id": str(uuid4())},
            )
            assert conflict.status_code == 409
            assert (
                len(
                    (
                        await client.get(
                            f"/api/v1/session-design-application-runs/{run_id}/steps"
                        )
                    ).json()
                )
                == 1
            )
        await stop_api_process(active_apis.pop())

        for worker_id, outcome in (
            ("session-load", "SUCCEEDED"),
            ("session-validate", "SUCCEEDED"),
            ("session-build", "SUCCEEDED"),
            ("session-verify", "SUCCEEDED"),
            ("session-wait", "WAITING_USER"),
        ):
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr
            assert stdout.strip() == outcome

        api_b = await start_api_process(environment)
        active_apis.append(api_b)
        async with AsyncClient(base_url=api_b.base_url) as client:
            waiting = await client.get(
                f"/api/v1/session-design-application-runs/{run_id}"
            )
            assert waiting.json()["status"] == "WAITING_CONFIRMATION"
            steps = (
                await client.get(
                    f"/api/v1/session-design-application-runs/{run_id}/steps"
                )
            ).json()
            assert len(steps) == 5
            build = next(
                item
                for item in steps
                if item["step_type"] == "BUILD_SESSION_PLAN_REVISION"
            )
            confirm = {
                "expected_revision": build["output_payload"]["created_revision"],
                "expected_plan_version": 1,
            }
            first = await client.post(
                f"/api/v1/session-design-application-runs/{run_id}/confirm",
                json=confirm,
            )
            retry = await client.post(
                f"/api/v1/session-design-application-runs/{run_id}/confirm",
                json=confirm,
            )
            assert first.status_code == retry.status_code == 202
            resumed = (
                await client.get(
                    f"/api/v1/session-design-application-runs/{run_id}/steps"
                )
            ).json()
            assert (
                sum(
                    item["step_type"] == "FINALIZE_SESSION_APPLICATION"
                    for item in resumed
                )
                == 1
            )
        await stop_api_process(active_apis.pop())

        code, stdout, stderr = await run_worker_once(environment, "session-finalize")
        assert code == 0, stderr
        assert stdout.strip() == "SUCCEEDED"
        api_c = await start_api_process(environment)
        active_apis.append(api_c)
        async with AsyncClient(base_url=api_c.base_url) as client:
            completed = await client.get(
                f"/api/v1/session-design-application-runs/{run_id}"
            )
            assert completed.json()["status"] == "COMPLETED"
            final_steps = (
                await client.get(
                    f"/api/v1/session-design-application-runs/{run_id}/steps"
                )
            ).json()
            checkpoints = (
                await client.get(
                    f"/api/v1/session-design-application-runs/{run_id}/checkpoints"
                )
            ).json()
            assert len(final_steps) == len(checkpoints) == 6
            revisions = await client.get(
                f"/api/v1/plans/{payload['root_plan_id']}/revisions"
            )
            assert [item["is_current_revision"] for item in revisions.json()] == [
                False,
                True,
            ]
        await stop_api_process(active_apis.pop())
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        await _cleanup_session_orchestration(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_design_revision_crash_reaper_resume_is_exactly_once(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    """A post-Build child exit leaves one committed Revision for fenced replay."""

    token = uuid4().hex
    email = f"session-revision-crash-{token}@fitweek.test"
    environment = cli_environment(database_url=mysql_test_url, email=email)
    environment.update(
        {
            "APP_ENV": "test",
            "MODEL_GATEWAY_ENABLED": "false",
            "ORCHESTRATOR_LEASE_SECONDS": "1",
        }
    )
    active_apis = []
    user_id: UUID | None = None
    try:
        api = await start_api_process(environment)
        active_apis.append(api)
        async with AsyncClient(base_url=api.base_url) as client:
            setup = await _setup_accepted_session_design(client, token)
            user_id = setup["user_id"]
            assert isinstance(user_id, UUID)
            payload = _run_payload(setup, token)
            created = await client.post(
                "/api/v1/session-design-application-runs", json=payload
            )
            assert created.status_code == 202, created.text
            assert UUID(created.json()["id"])
        await stop_api_process(active_apis.pop())

        for worker_id in ("session-crash-load", "session-crash-validate"):
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, type(stderr).__name__
            assert stdout.strip() == "SUCCEEDED"

        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "tests/support/session_design_revision_apply_crash_helper.py",
            "session-revision-crash-child",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=30)
        assert child.returncode == 0, type(stderr.decode()).__name__
        crash = json.loads(stdout.decode())
        build_step_id = UUID(crash["step_id"])
        old_fence = int(crash["fencing_token"])

        async with mysql_test_database.session_factory() as session:
            build = await session.get(AgentStepModel, str(build_step_id))
            assert build is not None
            assert build.status == "RUNNING"
            assert build.attempt_count == 1
            assert build.fencing_token == old_fence
            assert (
                await session.scalar(
                    select(CheckpointModel.id).where(
                        CheckpointModel.step_id == str(build_step_id)
                    )
                )
                is None
            )
            assert (
                await session.scalar(
                    select(SessionDesignApplicationResultModel.id).where(
                        SessionDesignApplicationResultModel.user_id == str(user_id)
                    )
                )
                == crash["application_result_id"]
            )

        await asyncio.sleep(1.2)
        reaped_code, _reaped_stdout, reaped_stderr = await run_reaper_once(environment)
        assert reaped_code == 0, type(reaped_stderr).__name__
        code, stdout, stderr = await run_worker_once(
            environment, "session-crash-replay"
        )
        assert code == 0, type(stderr).__name__
        assert stdout.strip() == "SUCCEEDED"

        async with mysql_test_database.session_factory() as session:
            build = await session.get(AgentStepModel, str(build_step_id))
            assert build is not None
            assert build.status == "SUCCEEDED"
            assert build.attempt_count == 2
            assert build.fencing_token > old_fence
            checkpoints = (
                await session.scalars(
                    select(CheckpointModel).where(
                        CheckpointModel.step_id == str(build_step_id)
                    )
                )
            ).all()
            assert len(checkpoints) == 1
            results = (
                await session.scalars(
                    select(SessionDesignApplicationResultModel).where(
                        SessionDesignApplicationResultModel.user_id == str(user_id)
                    )
                )
            ).all()
            assert len(results) == 1
            assert results[0].id == crash["application_result_id"]
            assert results[0].created_revision == 2

        api = await start_api_process(environment)
        active_apis.append(api)
        async with AsyncClient(base_url=api.base_url) as client:
            revisions = await client.get(
                f"/api/v1/plans/{payload['root_plan_id']}/revisions"
            )
            assert revisions.status_code == 200
            assert [item["plan"]["revision"] for item in revisions.json()] == [1, 2]
        await stop_api_process(active_apis.pop())
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        await _cleanup_session_orchestration(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_design_run_cancel_is_terminal_across_restart(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    """A restarted API retains the unconfirmed Revision when cancel is repeated."""

    token = uuid4().hex
    email = f"session-cancel-{token}@fitweek.test"
    environment = cli_environment(database_url=mysql_test_url, email=email)
    environment.update({"APP_ENV": "test", "MODEL_GATEWAY_ENABLED": "false"})
    active_apis = []
    user_id: UUID | None = None
    try:
        api = await start_api_process(environment)
        active_apis.append(api)
        async with AsyncClient(base_url=api.base_url) as client:
            setup = await _setup_accepted_session_design(client, token)
            user_id = setup["user_id"]
            assert isinstance(user_id, UUID)
            payload = _run_payload(setup, token)
            created = await client.post(
                "/api/v1/session-design-application-runs", json=payload
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["id"]
        await stop_api_process(active_apis.pop())

        for worker_id, outcome in (
            ("session-cancel-load", "SUCCEEDED"),
            ("session-cancel-validate", "SUCCEEDED"),
            ("session-cancel-build", "SUCCEEDED"),
            ("session-cancel-verify", "SUCCEEDED"),
            ("session-cancel-wait", "WAITING_USER"),
        ):
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, type(stderr).__name__
            assert stdout.strip() == outcome

        api = await start_api_process(environment)
        active_apis.append(api)
        async with AsyncClient(base_url=api.base_url) as client:
            waiting = await client.get(
                f"/api/v1/session-design-application-runs/{run_id}"
            )
            assert waiting.status_code == 200
            assert waiting.json()["status"] == "WAITING_CONFIRMATION"
            before_steps = (
                await client.get(
                    f"/api/v1/session-design-application-runs/{run_id}/steps"
                )
            ).json()
            before_checkpoints = (
                await client.get(
                    f"/api/v1/session-design-application-runs/{run_id}/checkpoints"
                )
            ).json()
            assert len(before_steps) == 5
            assert len(before_checkpoints) == 4
        await stop_api_process(active_apis.pop())

        api = await start_api_process(environment)
        active_apis.append(api)
        async with AsyncClient(base_url=api.base_url) as client:
            first = await client.post(
                f"/api/v1/session-design-application-runs/{run_id}/cancel"
            )
            second = await client.post(
                f"/api/v1/session-design-application-runs/{run_id}/cancel"
            )
            assert first.status_code == second.status_code == 200
            assert first.json()["status"] == second.json()["status"] == "CANCELLED"
        await stop_api_process(active_apis.pop())

        code, stdout, stderr = await run_worker_once(
            environment, "session-cancel-empty"
        )
        assert code == 0, type(stderr).__name__
        assert stdout.strip() == "EMPTY"

        api = await start_api_process(environment)
        active_apis.append(api)
        async with AsyncClient(base_url=api.base_url) as client:
            cancelled = await client.get(
                f"/api/v1/session-design-application-runs/{run_id}"
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "CANCELLED"
            after_steps = (
                await client.get(
                    f"/api/v1/session-design-application-runs/{run_id}/steps"
                )
            ).json()
            after_checkpoints = (
                await client.get(
                    f"/api/v1/session-design-application-runs/{run_id}/checkpoints"
                )
            ).json()
            assert not any(
                step["step_type"] == "FINALIZE_SESSION_APPLICATION"
                for step in after_steps
            )
            assert [step["id"] for step in after_steps[:4]] == [
                step["id"] for step in before_steps[:4]
            ]
            assert [checkpoint["id"] for checkpoint in after_checkpoints] == [
                checkpoint["id"] for checkpoint in before_checkpoints
            ]
            revisions = await client.get(
                f"/api/v1/plans/{payload['root_plan_id']}/revisions"
            )
            assert revisions.status_code == 200
            history = revisions.json()
            assert [item["plan"]["status"] for item in history] == [
                "CONFIRMED",
                "VALIDATED",
            ]
            assert [item["is_current_revision"] for item in history] == [True, False]

        async with mysql_test_database.session_factory() as session:
            application_results = (
                await session.scalars(
                    select(SessionDesignApplicationResultModel).where(
                        SessionDesignApplicationResultModel.user_id == str(user_id)
                    )
                )
            ).all()
            assert len(application_results) == 1
            draft = await session.get(SessionDesignDraftModel, payload["draft_id"])
            assert draft is not None
            assert draft.status == "APPLIED"
            assert draft.application_result_id == application_results[0].id
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        await _cleanup_session_orchestration(mysql_test_database, user_id)
