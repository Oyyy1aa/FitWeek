"""Persistent ProfileDraft orchestration process-recovery contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select

import app.main as main_module
from app.config import PersistenceBackend, Settings
from app.domain.orchestration.enums import StepType
from app.orchestration.mysql_runtime import build_mysql_cli_runtime
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AgentStepModel,
    AuditEventModel,
    CheckpointModel,
    ContextSnapshotModel,
    FitnessProfileModel,
    IdempotencyRecordModel,
    PlanningRunModel,
    ProfileDraftModel,
    StepDependencyModel,
    UserAccountModel,
    UserConstraintModel,
)
from app.persistence.mysql.orchestration_repository import MySQLOrchestrationRepository
from app.persistence.mysql.profile_draft_repository import MySQLProfileDraftRepository
from tests.support.mysql_orchestrator_process import (
    cli_environment,
    run_profile_apply_then_exit,
    run_reaper_once,
    run_worker_once,
    start_api_process,
    start_profile_model_stub_process,
    stop_api_process,
)


def _mysql_settings(database_url: str, email: str) -> Settings:
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


async def _cleanup_user(database: Database, user_id: UUID | None) -> None:
    if user_id is None:
        return
    user_id_text = str(user_id)
    async with database.session_factory() as session:
        async with session.begin():
            run_ids = (
                await session.scalars(
                    select(PlanningRunModel.id).where(
                        PlanningRunModel.user_id == user_id_text
                    )
                )
            ).all()
            step_ids = (
                await session.scalars(
                    select(AgentStepModel.id).where(AgentStepModel.run_id.in_(run_ids))
                )
            ).all()
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id == user_id_text)
            )
            await session.execute(
                delete(ProfileDraftModel).where(
                    ProfileDraftModel.user_id == user_id_text
                )
            )
            await session.execute(
                delete(ContextSnapshotModel).where(
                    ContextSnapshotModel.user_id == user_id_text
                )
            )
            await session.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.user_id == user_id_text
                )
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
                        FitnessProfileModel.user_id == user_id_text
                    )
                )
            ).all()
            await session.execute(
                delete(UserConstraintModel).where(
                    UserConstraintModel.profile_id.in_(profile_ids)
                )
            )
            await session.execute(
                delete(FitnessProfileModel).where(
                    FitnessProfileModel.user_id == user_id_text
                )
            )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id == user_id_text)
            )


def _run_payload(token: str) -> dict[str, object]:
    return {
        "client_request_id": f"profile-run-{token}",
        "user_message": "Build a general fitness profile for home training.",
        "current_week": "2026-07-20",
    }


def _apply_payload(token: str) -> dict[str, object]:
    return {
        "client_request_id": f"profile-apply-{token}",
        "expected_draft_version": 1,
        "expected_profile_version": None,
        "accept_weekly_frequency": True,
        "accept_max_session_minutes": True,
        "selected_primary_goal": "GENERAL_FITNESS",
        "accepted_equipment": ["resistance_band"],
        "accepted_locations": ["HOME"],
        "accepted_hard_constraint_indexes": [0],
        "accepted_temporary_constraint_indexes": [0],
        "temporary_constraint_expirations": {
            "0": (datetime.now(UTC) + timedelta(days=7)).isoformat()
        },
        "confirmed_experience_level": "BEGINNER",
        "confirm_scope": True,
    }


async def _run_resource(
    client: AsyncClient, run_id: str, resource: str
) -> list[dict[str, Any]]:
    return (await client.get(f"/api/v1/profile-agent/runs/{run_id}/{resource}")).json()


async def _draft_status(client: AsyncClient, draft_id: str) -> str:
    return (await client.get(f"/api/v1/profile-agent/drafts/{draft_id}")).json()[
        "status"
    ]


def _profile_process_environment(
    *, database_url: str, email: str, stub_base_url: str
) -> dict[str, str]:
    environment = cli_environment(database_url=database_url, email=email)
    environment.update(
        {
            "MODEL_GATEWAY_ENABLED": "true",
            "MODEL_PRIMARY_PROVIDER": "http",
            "MODEL_PRIMARY_BASE_URL": f"{stub_base_url}/profile-apply",
            "MODEL_PRIMARY_API_KEY": "profile-test-key",
            "MODEL_PRIMARY_MODEL": "controlled-profile-draft",
            "MODEL_BACKUP_PROVIDER": "template-fallback",
        }
    )
    return environment


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_profile_run_dependency_never_uses_memory_container(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _mysql_settings(
        mysql_test_url, f"profile-run-dependency-{uuid4().hex}@fitweek.test"
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    application = main_module.create_application()
    user_id: UUID | None = None
    try:
        async with application.router.lifespan_context(application):
            assert application.state.business_container is None
            assert isinstance(
                application.state.profile_draft_repository,
                MySQLProfileDraftRepository,
            )
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
                created = await client.post(
                    "/api/v1/profile-agent/runs",
                    json={
                        "client_request_id": f"profile-run-{uuid4().hex}",
                        "user_message": "Create a general fitness profile draft.",
                        "current_week": "2026-07-20",
                    },
                )
                assert created.status_code == 202, created.text
                run_id = created.json()["run_id"]
                steps = await client.get(f"/api/v1/profile-agent/runs/{run_id}/steps")
                assert [item["status"] for item in steps.json()] == ["READY"]

        runtime, database = await build_mysql_cli_runtime(
            settings, worker_id="profile-run-dependency-cli"
        )
        try:
            assert runtime.profile_agent_run_service is not None
            for step_type in (
                StepType.PARSE_PROFILE_REQUEST,
                StepType.WAIT_FOR_PROFILE_DRAFT_REVIEW,
                StepType.APPLY_PROFILE_DRAFT,
                StepType.FINALIZE_PROFILE_RUN,
            ):
                runtime.registry.get(step_type)
        finally:
            if runtime.profile_agent_model_gateway is not None:
                await runtime.profile_agent_model_gateway.close()
            await database.dispose()
    finally:
        await _cleanup_user(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_draft_run_survives_api_worker_restarts_and_apply(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    token = uuid4().hex
    email = f"profile-run-apply-{token}@fitweek.test"
    active_apis = []
    user_id: UUID | None = None
    stub = None
    try:
        stub = await start_profile_model_stub_process()
        environment = _profile_process_environment(
            database_url=mysql_test_url,
            email=email,
            stub_base_url=stub.base_url,
        )
        api_a = await start_api_process(environment)
        active_apis.append(api_a)
        async with AsyncClient(base_url=api_a.base_url) as client:
            user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
            payload = _run_payload(token)
            created = await client.post("/api/v1/profile-agent/runs", json=payload)
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
            repeated = await client.post("/api/v1/profile-agent/runs", json=payload)
            assert repeated.status_code == 200
            assert repeated.json()["run_id"] == run_id
            conflict = await client.post(
                "/api/v1/profile-agent/runs",
                json={**payload, "user_message": "A changed request must conflict."},
            )
            assert conflict.status_code == 409
            initial_steps = await _run_resource(client, run_id, "steps")
            assert [item["status"] for item in initial_steps] == ["READY"]
            model_count = await AsyncClient(base_url=stub.base_url).__aenter__()
            try:
                assert (await model_count.get("/admin/count/profile-apply")).json() == {
                    "count": 0
                }
            finally:
                await model_count.aclose()
        await stop_api_process(active_apis.pop())

        for worker_id, outcome in (
            ("profile-parse", "SUCCEEDED"),
            ("profile-wait", "WAITING_USER"),
        ):
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr
            assert stdout.strip() == outcome

        api_b = await start_api_process(environment)
        active_apis.append(api_b)
        async with AsyncClient(base_url=api_b.base_url) as client:
            waiting = await client.get(f"/api/v1/profile-agent/runs/{run_id}")
            assert waiting.json()["status"] == "WAITING_PROFILE_REVIEW"
            steps = await _run_resource(client, run_id, "steps")
            checkpoints = await _run_resource(client, run_id, "checkpoints")
            draft_id = steps[1]["output_payload"]["draft_id"]
            assert len(steps) == 2
            assert len(checkpoints) == 1
            assert await _draft_status(client, draft_id) == "PENDING_REVIEW"
            decision = _apply_payload(token)
            first = await client.post(
                f"/api/v1/profile-agent/runs/{run_id}/apply", json=decision
            )
            retry = await client.post(
                f"/api/v1/profile-agent/runs/{run_id}/apply", json=decision
            )
            assert first.status_code == retry.status_code == 202
            resumed_steps = await _run_resource(client, run_id, "steps")
            assert (
                sum(
                    item["step_type"] == "APPLY_PROFILE_DRAFT" for item in resumed_steps
                )
                == 1
            )
        await stop_api_process(active_apis.pop())

        for worker_id in ("profile-apply", "profile-finalize"):
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr
            assert stdout.strip() == "SUCCEEDED"

        api_c = await start_api_process(environment)
        active_apis.append(api_c)
        async with AsyncClient(base_url=api_c.base_url) as client:
            completed = await client.get(f"/api/v1/profile-agent/runs/{run_id}")
            steps = await _run_resource(client, run_id, "steps")
            checkpoints = await _run_resource(client, run_id, "checkpoints")
            audit = await _run_resource(client, run_id, "audit")
            draft = await client.get(f"/api/v1/profile-agent/drafts/{draft_id}")
            profile = await client.get("/api/v1/profiles/me")
            assert completed.json()["status"] == "COMPLETED"
            assert len(steps) == len(checkpoints) == 4
            assert draft.json()["status"] == "APPLIED"
            assert profile.status_code == 200
            assert [item["sequence_no"] for item in audit] == list(
                range(1, len(audit) + 1)
            )
            async with AsyncClient(base_url=stub.base_url) as stub_client:
                assert (await stub_client.get("/admin/count/profile-apply")).json() == {
                    "count": 1
                }
        await stop_api_process(active_apis.pop())
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        if stub is not None:
            await stop_api_process(stub)
        await _cleanup_user(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_draft_run_reject_is_terminal_across_restart(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    token = uuid4().hex
    active_apis = []
    user_id: UUID | None = None
    stub = None
    try:
        stub = await start_profile_model_stub_process()
        environment = _profile_process_environment(
            database_url=mysql_test_url,
            email=f"profile-run-reject-{token}@fitweek.test",
            stub_base_url=stub.base_url,
        )
        api_a = await start_api_process(environment)
        active_apis.append(api_a)
        async with AsyncClient(base_url=api_a.base_url) as client:
            user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
            created = await client.post(
                "/api/v1/profile-agent/runs", json=_run_payload(token)
            )
            assert created.status_code == 202
            run_id = created.json()["run_id"]
        await stop_api_process(active_apis.pop())
        reject_workers = (
            ("reject-parse", "SUCCEEDED"),
            ("reject-wait", "WAITING_USER"),
        )
        for worker_id, outcome in reject_workers:
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr
            assert stdout.strip() == outcome
        api_b = await start_api_process(environment)
        active_apis.append(api_b)
        async with AsyncClient(base_url=api_b.base_url) as client:
            waiting_steps = await _run_resource(client, run_id, "steps")
            predecessor_checkpoints = await _run_resource(client, run_id, "checkpoints")
            draft_id = waiting_steps[1]["output_payload"]["draft_id"]
            reject_payload = {
                "client_request_id": f"profile-reject-{token}",
                "expected_draft_version": 1,
            }
            first = await client.post(
                f"/api/v1/profile-agent/runs/{run_id}/reject", json=reject_payload
            )
            retry = await client.post(
                f"/api/v1/profile-agent/runs/{run_id}/reject", json=reject_payload
            )
            assert first.status_code == retry.status_code == 202
            assert first.json()["status"] == retry.json()["status"] == "CANCELLED"
        await stop_api_process(active_apis.pop())
        code, stdout, stderr = await run_worker_once(environment, "reject-after")
        assert code == 0, stderr
        assert stdout.strip() == "EMPTY"
        api_c = await start_api_process(environment)
        active_apis.append(api_c)
        async with AsyncClient(base_url=api_c.base_url) as client:
            cancelled = await client.get(f"/api/v1/profile-agent/runs/{run_id}")
            steps = await _run_resource(client, run_id, "steps")
            checkpoints = await _run_resource(client, run_id, "checkpoints")
            draft = await client.get(f"/api/v1/profile-agent/drafts/{draft_id}")
            assert cancelled.json()["status"] == "CANCELLED"
            assert steps[:1] == waiting_steps[:1]
            assert steps[1]["status"] == "CANCELLED"
            assert len(steps) == 2
            assert checkpoints == predecessor_checkpoints
            assert draft.json()["status"] == "REJECTED"
            assert (await client.get("/api/v1/profiles/me")).status_code == 404
        await stop_api_process(active_apis.pop())
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        if stub is not None:
            await stop_api_process(stub)
        await _cleanup_user(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_draft_apply_crash_reaper_resume_is_exactly_once(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    token = uuid4().hex
    active_apis = []
    user_id: UUID | None = None
    stub = None
    try:
        stub = await start_profile_model_stub_process()
        environment = _profile_process_environment(
            database_url=mysql_test_url,
            email=f"profile-run-crash-{token}@fitweek.test",
            stub_base_url=stub.base_url,
        )
        environment["ORCHESTRATOR_LEASE_SECONDS"] = "1"
        api_a = await start_api_process(environment)
        active_apis.append(api_a)
        async with AsyncClient(base_url=api_a.base_url) as client:
            user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
            run_id = (
                await client.post(
                    "/api/v1/profile-agent/runs", json=_run_payload(token)
                )
            ).json()["run_id"]
        await stop_api_process(active_apis.pop())
        crash_workers = (
            ("crash-parse", "SUCCEEDED"),
            ("crash-wait", "WAITING_USER"),
        )
        for worker_id, outcome in crash_workers:
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr
            assert stdout.strip() == outcome
        api_b = await start_api_process(environment)
        active_apis.append(api_b)
        async with AsyncClient(base_url=api_b.base_url) as client:
            steps = await _run_resource(client, run_id, "steps")
            draft_id = steps[1]["output_payload"]["draft_id"]
            applied = await client.post(
                f"/api/v1/profile-agent/runs/{run_id}/apply",
                json=_apply_payload(token),
            )
            assert applied.status_code == 202
        await stop_api_process(active_apis.pop())
        code, stdout, stderr = await run_profile_apply_then_exit(
            environment, "crash-apply"
        )
        assert code == 0, stderr
        crashed = json.loads(stdout)
        old_fence = crashed["fencing_token"]
        api_c = await start_api_process(environment)
        active_apis.append(api_c)
        async with AsyncClient(base_url=api_c.base_url) as client:
            steps = await _run_resource(client, run_id, "steps")
            checkpoints = await _run_resource(client, run_id, "checkpoints")
            assert steps[2]["status"] == "RUNNING"
            assert len(checkpoints) == 2
            assert await _draft_status(client, draft_id) == "APPLIED"
            profile = await client.get("/api/v1/profiles/me")
            assert profile.status_code == 200
        await stop_api_process(active_apis.pop())
        await asyncio.sleep(1.2)
        code, _stdout, stderr = await run_reaper_once(environment)
        assert code == 0, stderr
        code, stdout, stderr = await run_worker_once(environment, "crash-replay")
        assert code == 0, stderr
        assert stdout.strip() == "SUCCEEDED"
        code, stdout, stderr = await run_worker_once(environment, "crash-finalize")
        assert code == 0, stderr
        assert stdout.strip() == "SUCCEEDED"
        api_d = await start_api_process(environment)
        active_apis.append(api_d)
        async with AsyncClient(base_url=api_d.base_url) as client:
            steps = await _run_resource(client, run_id, "steps")
            checkpoints = await _run_resource(client, run_id, "checkpoints")
            assert steps[2]["status"] == "SUCCEEDED"
            assert steps[2]["attempt_count"] == 2
            durable_steps = await MySQLOrchestrationRepository(
                mysql_test_database.session_factory
            ).list_steps(UUID(run_id))
            assert durable_steps[2].fencing_token > old_fence
            assert len(checkpoints) == 4
            assert await _draft_status(client, draft_id) == "APPLIED"
            async with AsyncClient(base_url=stub.base_url) as stub_client:
                assert (await stub_client.get("/admin/count/profile-apply")).json() == {
                    "count": 1
                }
        await stop_api_process(active_apis.pop())
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        if stub is not None:
            await stop_api_process(stub)
        await _cleanup_user(mysql_test_database, user_id)
