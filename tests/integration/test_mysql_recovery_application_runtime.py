"""MySQL Recovery Application runtime composition contracts."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text

import app.api.dependencies as dependencies_module
import app.main as main_module
from app.application.recovery_application_orchestration import (
    RecoveryApplicationRunService,
)
from app.application.recovery_applications import RecoveryApplicationService
from app.config import PersistenceBackend, Settings
from app.domain.orchestration.enums import StepType
from app.orchestration.mysql_runtime import build_mysql_cli_runtime
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AgentStepModel,
    CheckpointModel,
    RecoveryApplicationResultModel,
)
from app.persistence.mysql.recovery_application_repository import (
    MySQLRecoveryApplicationRepository,
)
from tests.integration.test_mysql_session_design_draft_runtime import (
    _generation_payload,
)
from tests.support.mysql_orchestrator_process import (
    cli_environment,
    run_reaper_once,
    run_worker_once,
    start_api_process,
    stop_api_process,
)

pytestmark = pytest.mark.integration


def _mysql_settings(database_url: str, email: str) -> Settings:
    return Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(database_url),
        redis_enabled=False,
        model_gateway_enabled=False,
        calendar_read_enabled=False,
        orchestrator_enabled=True,
        single_user_email=email,
        _env_file=None,
    )


async def _cleanup_user(database: Database, user_id: UUID | None) -> None:
    if user_id is None:
        return
    owner = str(user_id)
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            try:
                for statement in (
                    """
                    DELETE step_dependency FROM step_dependency
                    JOIN agent_step ON step_dependency.step_id = agent_step.id
                    JOIN planning_run ON agent_step.run_id = planning_run.id
                    WHERE planning_run.user_id = :user_id
                    """,
                    """
                    DELETE planning_checkpoint FROM planning_checkpoint
                    JOIN planning_run ON planning_checkpoint.run_id = planning_run.id
                    WHERE planning_run.user_id = :user_id
                    """,
                    """
                    DELETE calendar_operation_item FROM calendar_operation_item
                    JOIN calendar_operation_draft
                    ON calendar_operation_item.draft_id = calendar_operation_draft.id
                    WHERE calendar_operation_draft.user_id = :user_id
                    """,
                    """
                    DELETE memory_evidence FROM memory_evidence
                    JOIN memory_item ON memory_evidence.memory_id = memory_item.id
                    WHERE memory_item.user_id = :user_id
                    """,
                    """
                    DELETE schedule_trace FROM schedule_trace
                    JOIN schedule_draft ON schedule_trace.draft_id = schedule_draft.id
                    WHERE schedule_draft.user_id = :user_id
                    """,
                    """
                    DELETE schedule_candidate_slot FROM schedule_candidate_slot
                    JOIN schedule_candidate_set
                    ON schedule_candidate_slot.candidate_set_id
                    = schedule_candidate_set.id
                    WHERE schedule_candidate_set.user_id = :user_id
                    """,
                    """
                    DELETE schedule_busy_interval FROM schedule_busy_interval
                    JOIN schedule_busy_snapshot
                    ON schedule_busy_interval.snapshot_id = schedule_busy_snapshot.id
                    WHERE schedule_busy_snapshot.user_id = :user_id
                    """,
                    """
                    DELETE session_design_trace FROM session_design_trace
                    JOIN session_design_draft
                    ON session_design_trace.draft_id = session_design_draft.id
                    WHERE session_design_draft.user_id = :user_id
                    """,
                    """
                    DELETE session_design_candidate_slot
                    FROM session_design_candidate_slot
                    JOIN session_design_candidate_set
                    ON session_design_candidate_slot.candidate_set_id
                    = session_design_candidate_set.id
                    WHERE session_design_candidate_set.user_id = :user_id
                    """,
                    """
                    DELETE session_exercise FROM session_exercise
                    JOIN workout_session
                    ON session_exercise.session_id = workout_session.id
                    JOIN weekly_plan ON workout_session.plan_id = weekly_plan.id
                    WHERE weekly_plan.user_id = :user_id
                    """,
                    """
                    DELETE workout_session FROM workout_session
                    JOIN weekly_plan ON workout_session.plan_id = weekly_plan.id
                    WHERE weekly_plan.user_id = :user_id
                    """,
                    """
                    DELETE user_constraint FROM user_constraint
                    JOIN fitness_profile
                    ON user_constraint.profile_id = fitness_profile.id
                    WHERE fitness_profile.user_id = :user_id
                    """,
                ):
                    await session.execute(text(statement), {"user_id": owner})
                for table in (
                    "audit_event",
                    "calendar_event_binding",
                    "calendar_operation_attempt",
                    "calendar_operation_draft",
                    "context_snapshot",
                    "fitness_profile",
                    "ics_export",
                    "idempotency_record",
                    "memory_candidate",
                    "memory_item",
                    "outbox",
                    "planning_run",
                    "profile_draft",
                    "recovery_action_candidate",
                    "recovery_application_result",
                    "recovery_behavior_summary",
                    "recovery_candidate_set",
                    "recovery_change_impact",
                    "recovery_draft",
                    "recovery_memory_proposal",
                    "recovery_memory_proposal_import",
                    "recovery_schedule_subdraft_binding",
                    "recovery_session_design_subdraft_binding",
                    "recovery_trace",
                    "schedule_application_result",
                    "schedule_availability_window",
                    "schedule_busy_snapshot",
                    "schedule_candidate_set",
                    "schedule_draft",
                    "session_checkin",
                    "session_design_application_result",
                    "session_design_candidate_set",
                    "session_design_draft",
                    "weekly_plan",
                    "user_account",
                ):
                    await session.execute(
                        text(f"DELETE FROM {table} WHERE user_id = :user_id")
                        if table != "user_account"
                        else text("DELETE FROM user_account WHERE id = :user_id"),
                        {"user_id": owner},
                    )
            finally:
                await session.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


async def _setup_accepted_recovery(
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
    created = await client.post(
        "/api/v1/recovery-drafts",
        json={
            "client_request_id": f"recovery-draft-{token}",
            "root_plan_id": source["root_plan_id"] or source["id"],
            "source_revision": source["revision"],
            "expected_plan_version": source["version"],
            "request_type": "RESCHEDULE_REQUEST",
            "target_session_ids": [source["sessions"][1]["id"]],
            "user_request": "Apply a controlled future-week adjustment.",
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    accepted = await client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/accept",
        json={"expected_version": draft["version"]},
    )
    assert accepted.status_code == 200, accepted.text
    return {"user_id": user_id, "plan": source, "draft": accepted.json()}


def _run_payload(setup: dict[str, object], token: str) -> dict[str, object]:
    plan = setup["plan"]
    draft = setup["draft"]
    assert isinstance(plan, dict) and isinstance(draft, dict)
    return {
        "client_request_id": f"recovery-run-{token}",
        "recovery_draft_id": draft["id"],
        "expected_draft_version": draft["version"],
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
    }


async def _accept_recovery_children(client: AsyncClient, run_id: str) -> None:
    children = (
        await client.get(f"/api/v1/recovery-application-runs/{run_id}/subdrafts")
    ).json()
    for draft_id in children["session_design_draft_ids"]:
        draft = (await client.get(f"/api/v1/session-designs/{draft_id}")).json()
        accepted = await client.post(
            f"/api/v1/session-designs/{draft_id}/accept",
            json={"expected_version": draft["version"]},
        )
        assert accepted.status_code == 200, accepted.text
    for draft_id in children["schedule_draft_ids"]:
        draft = (await client.get(f"/api/v1/schedule-drafts/{draft_id}")).json()
        accepted = await client.post(
            f"/api/v1/schedule-drafts/{draft_id}/accept",
            json={"expected_version": draft["version"]},
        )
        assert accepted.status_code == 200, accepted.text


@pytest.mark.asyncio
async def test_mysql_recovery_application_api_and_worker_never_use_memory_container(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both MySQL processes compose Recovery from typed durable adapters."""

    settings = _mysql_settings(
        mysql_test_url,
        f"recovery-application-runtime-{uuid4().hex}@fitweek.test",
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    application = main_module.create_application()
    user_id: UUID | None = None
    try:
        async with application.router.lifespan_context(application):
            user_id = application.state.local_user.id
            assert application.state.business_container is None

            api_service = application.state.recovery_application_service
            api_run_service = application.state.recovery_application_run_service
            assert isinstance(api_service, RecoveryApplicationService)
            assert isinstance(api_run_service, RecoveryApplicationRunService)
            assert isinstance(
                api_service._applications,  # noqa: SLF001 - composition contract
                MySQLRecoveryApplicationRepository,
            )

            request = SimpleNamespace(app=application)
            assert (
                dependencies_module.get_recovery_application_service(request)
                is api_service
            )
            assert (
                dependencies_module.get_recovery_application_run_service(request)
                is api_run_service
            )

        runtime, database = await build_mysql_cli_runtime(
            settings,
            worker_id="recovery-application-composition-cli",
        )
        try:
            assert isinstance(
                runtime.recovery_application_service,
                RecoveryApplicationService,
            )
            assert isinstance(
                runtime.recovery_application_run_service,
                RecoveryApplicationRunService,
            )
            assert isinstance(
                runtime.recovery_application_service._applications,  # noqa: SLF001
                MySQLRecoveryApplicationRepository,
            )
            for step_type in (
                StepType.LOAD_RECOVERY_APPLICATION_CONTEXT,
                StepType.VALIDATE_RECOVERY_DRAFT,
                StepType.RESOLVE_RECOVERY_ACTIONS,
                StepType.CREATE_RECOVERY_SUBDRAFTS,
                StepType.WAIT_FOR_RECOVERY_SUBDRAFT_REVIEWS,
                StepType.BUILD_RECOVERY_PLAN_REVISION,
                StepType.VERIFY_RECOVERY_PLAN_SAFETY,
                StepType.WAIT_FOR_RECOVERY_REVISION_CONFIRMATION,
                StepType.FINALIZE_RECOVERY_APPLICATION,
            ):
                runtime.registry.get(step_type)
        finally:
            await database.dispose()
    finally:
        await _cleanup_user(mysql_test_database, user_id)


@pytest.mark.asyncio
async def test_recovery_run_survives_api_worker_restarts_and_is_idempotent(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    """A durable Recovery Run crosses API/Worker processes without replaying work."""

    token = uuid4().hex
    environment = cli_environment(
        database_url=mysql_test_url,
        email=f"recovery-run-{token}@fitweek.test",
    )
    environment.update(
        {
            "APP_ENV": "test",
            "MODEL_GATEWAY_ENABLED": "false",
            "CALENDAR_READ_ENABLED": "false",
        }
    )
    active_apis = []
    user_id: UUID | None = None
    try:
        api_a = await start_api_process(environment)
        active_apis.append(api_a)
        async with AsyncClient(base_url=api_a.base_url) as client:
            setup = await _setup_accepted_recovery(client, token)
            user_id = setup["user_id"]
            assert isinstance(user_id, UUID)
            payload = _run_payload(setup, token)
            created = await client.post(
                "/api/v1/recovery-application-runs", json=payload
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["id"]
            repeated = await client.post(
                "/api/v1/recovery-application-runs", json=payload
            )
            assert repeated.status_code == 200, repeated.text
            assert repeated.json()["id"] == run_id
            conflict = await client.post(
                "/api/v1/recovery-application-runs",
                json={**payload, "expected_plan_version": 999},
            )
            assert conflict.status_code == 409, conflict.text
        await stop_api_process(active_apis.pop())

        for worker_id, outcome in (
            ("recovery-load", "SUCCEEDED"),
            ("recovery-validate", "SUCCEEDED"),
            ("recovery-resolve", "SUCCEEDED"),
            ("recovery-subdrafts", "SUCCEEDED"),
            ("recovery-wait-children", "WAITING_USER"),
        ):
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr
            assert stdout.strip() == outcome

        api_b = await start_api_process(environment)
        active_apis.append(api_b)
        async with AsyncClient(base_url=api_b.base_url) as client:
            waiting = await client.get(f"/api/v1/recovery-application-runs/{run_id}")
            assert waiting.status_code == 200, waiting.text
            assert waiting.json()["status"] == "WAITING_SUBDRAFT_REVIEW"
            await _accept_recovery_children(client, run_id)
            resumed = await client.post(
                f"/api/v1/recovery-application-runs/{run_id}/continue"
            )
            assert resumed.status_code == 202, resumed.text
        await stop_api_process(active_apis.pop())

        for worker_id, outcome in (
            ("recovery-build", "SUCCEEDED"),
            ("recovery-verify", "SUCCEEDED"),
            ("recovery-wait-confirm", "WAITING_USER"),
        ):
            code, stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr
            assert stdout.strip() == outcome

        api_c = await start_api_process(environment)
        active_apis.append(api_c)
        async with AsyncClient(base_url=api_c.base_url) as client:
            steps = (
                await client.get(f"/api/v1/recovery-application-runs/{run_id}/steps")
            ).json()
            confirmation = next(
                item
                for item in steps
                if item["step_type"] == "WAIT_FOR_RECOVERY_REVISION_CONFIRMATION"
            )
            output = confirmation["output_payload"]
            confirmed = await client.post(
                f"/api/v1/recovery-application-runs/{run_id}/confirm",
                json={
                    "expected_revision": output["created_revision"],
                    "expected_plan_version": output["resulting_plan_version"],
                },
            )
            repeated_confirmation = await client.post(
                f"/api/v1/recovery-application-runs/{run_id}/confirm",
                json={
                    "expected_revision": output["created_revision"],
                    "expected_plan_version": output["resulting_plan_version"],
                },
            )
            assert confirmed.status_code == repeated_confirmation.status_code == 202
            final_steps = (
                await client.get(f"/api/v1/recovery-application-runs/{run_id}/steps")
            ).json()
            assert (
                sum(
                    item["step_type"] == "FINALIZE_RECOVERY_APPLICATION"
                    for item in final_steps
                )
                == 1
            )
        await stop_api_process(active_apis.pop())

        code, stdout, stderr = await run_worker_once(environment, "recovery-finalize")
        assert code == 0, stderr
        assert stdout.strip() == "SUCCEEDED"

        api_d = await start_api_process(environment)
        active_apis.append(api_d)
        async with AsyncClient(base_url=api_d.base_url) as client:
            completed = await client.get(f"/api/v1/recovery-application-runs/{run_id}")
            assert completed.status_code == 200, completed.text
            assert completed.json()["status"] == "COMPLETED"
            steps = (
                await client.get(f"/api/v1/recovery-application-runs/{run_id}/steps")
            ).json()
            checkpoints = (
                await client.get(
                    f"/api/v1/recovery-application-runs/{run_id}/checkpoints"
                )
            ).json()
            assert len(steps) == len(checkpoints) == 9
            revisions = await client.get(
                f"/api/v1/plans/{payload['root_plan_id']}/revisions"
            )
            assert revisions.status_code == 200, revisions.text
            assert [item["plan"]["revision"] for item in revisions.json()] == [1, 2]
        await stop_api_process(active_apis.pop())
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        await _cleanup_user(mysql_test_database, user_id)


@pytest.mark.asyncio
async def test_recovery_commit_before_checkpoint_reclaims_without_duplicate_apply(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    """A reclaimed Build step observes the committed MySQL operation exactly once."""

    token = uuid4().hex
    environment = cli_environment(
        database_url=mysql_test_url,
        email=f"recovery-crash-{token}@fitweek.test",
    )
    environment.update(
        {
            "APP_ENV": "test",
            "MODEL_GATEWAY_ENABLED": "false",
            "CALENDAR_READ_ENABLED": "false",
            "ORCHESTRATOR_LEASE_SECONDS": "1",
        }
    )
    active_apis = []
    user_id: UUID | None = None
    try:
        api = await start_api_process(environment)
        active_apis.append(api)
        async with AsyncClient(base_url=api.base_url) as client:
            setup = await _setup_accepted_recovery(client, token)
            user_id = setup["user_id"]
            assert isinstance(user_id, UUID)
            payload = _run_payload(setup, token)
            created = await client.post(
                "/api/v1/recovery-application-runs", json=payload
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["id"]
        await stop_api_process(active_apis.pop())

        for worker_id in (
            "recovery-crash-load",
            "recovery-crash-validate",
            "recovery-crash-resolve",
            "recovery-crash-subdrafts",
            "recovery-crash-wait-children",
        ):
            code, _stdout, stderr = await run_worker_once(environment, worker_id)
            assert code == 0, stderr

        api = await start_api_process(environment)
        active_apis.append(api)
        async with AsyncClient(base_url=api.base_url) as client:
            await _accept_recovery_children(client, run_id)
            resumed = await client.post(
                f"/api/v1/recovery-application-runs/{run_id}/continue"
            )
            assert resumed.status_code == 202, resumed.text
        await stop_api_process(active_apis.pop())

        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "tests/support/recovery_application_revision_apply_crash_helper.py",
            "recovery-build-crash-child",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=30)
        assert child.returncode == 0, stderr.decode()
        crash = json.loads(stdout.decode())
        step_id = crash["step_id"]
        old_fence = int(crash["fencing_token"])

        async with mysql_test_database.session_factory() as session:
            build = await session.get(AgentStepModel, step_id)
            assert build is not None
            assert build.status == "RUNNING"
            assert build.attempt_count == 1
            assert build.fencing_token == old_fence
            assert (
                await session.scalar(
                    select(CheckpointModel.id).where(CheckpointModel.step_id == step_id)
                )
                is None
            )
            results = (
                await session.scalars(
                    select(RecoveryApplicationResultModel).where(
                        RecoveryApplicationResultModel.user_id == str(user_id)
                    )
                )
            ).all()
            assert [item.id for item in results] == [crash["application_result_id"]]

        await asyncio.sleep(1.2)
        code, _stdout, stderr = await run_reaper_once(environment)
        assert code == 0, stderr
        code, stdout, stderr = await run_worker_once(
            environment, "recovery-build-reclaim"
        )
        assert code == 0, stderr
        assert stdout.strip() == "SUCCEEDED"

        async with mysql_test_database.session_factory() as session:
            build = await session.get(AgentStepModel, step_id)
            assert build is not None
            assert build.status == "SUCCEEDED"
            assert build.attempt_count == 2
            assert build.fencing_token > old_fence
            checkpoints = (
                await session.scalars(
                    select(CheckpointModel).where(CheckpointModel.step_id == step_id)
                )
            ).all()
            assert len(checkpoints) == 1
            results = (
                await session.scalars(
                    select(RecoveryApplicationResultModel).where(
                        RecoveryApplicationResultModel.user_id == str(user_id)
                    )
                )
            ).all()
            assert len(results) == 1
            assert results[0].id == crash["application_result_id"]
            assert results[0].created_revision == 2
    finally:
        while active_apis:
            await stop_api_process(active_apis.pop())
        await _cleanup_user(mysql_test_database, user_id)
