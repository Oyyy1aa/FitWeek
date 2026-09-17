"""Red-first MySQL Calendar Run control-plane composition coverage."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select

import app.main as main_module
from app.api.dependencies import get_calendar_operation_run_service
from app.application.calendar_operation_orchestration import CalendarOperationRunService
from app.domain.orchestration.enums import StepType
from app.orchestration.reaper import StepReaper
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
from tests.integration.test_mysql_calendar_draft_review_runtime import (
    _cleanup,
    _confirmed_plan,
    _draft_payload,
    _mysql_api_environment,
)
from tests.support.mysql_orchestrator_process import (
    ApiProcess,
    start_api_process,
    stop_api_process,
)


async def _cleanup_run_control_plane(database: Database, user_id: UUID | None) -> None:
    """Delete only this generated user's orchestration facts before Plan cleanup."""
    if user_id is None:
        return
    async with database.session_factory() as session:
        async with session.begin():
            run_ids = (
                await session.scalars(
                    select(PlanningRunModel.id).where(
                        PlanningRunModel.user_id == str(user_id)
                    )
                )
            ).all()
            if run_ids:
                step_ids = (
                    await session.scalars(
                        select(AgentStepModel.id).where(
                            AgentStepModel.run_id.in_(run_ids)
                        )
                    )
                ).all()
                if step_ids:
                    await session.execute(
                        delete(StepDependencyModel).where(
                            StepDependencyModel.step_id.in_(step_ids)
                            | StepDependencyModel.dependency_step_id.in_(step_ids)
                        )
                    )
                    await session.execute(
                        delete(CheckpointModel).where(
                            CheckpointModel.step_id.in_(step_ids)
                        )
                    )
                await session.execute(
                    delete(AuditEventModel).where(AuditEventModel.run_id.in_(run_ids))
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
async def test_mysql_calendar_run_dependencies_are_durable_providerless_and_worker_not_started(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """MySQL lifespan must expose Run enqueue without memory or a worker pool."""

    monkeypatch.setenv("PERSISTENCE_BACKEND", "mysql")
    monkeypatch.setenv("DATABASE_URL", mysql_test_url)
    monkeypatch.setenv("SINGLE_USER_EMAIL", f"calendar-run-{uuid4().hex}@fitweek.test")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "true")
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    application = main_module.create_application()
    user_id: UUID | None = None
    reaper_started = False

    async def reaper_sentinel(self):  # type: ignore[no-untyped-def]
        nonlocal reaper_started
        reaper_started = True

    monkeypatch.setattr(StepReaper, "run_once", reaper_sentinel)
    try:
        async with application.router.lifespan_context(application):
            user_id = application.state.local_user.id
            assert application.state.business_container is None
            service = application.state.calendar_operation_run_service
            assert isinstance(service, CalendarOperationRunService)
            assert isinstance(service._repository, MySQLOrchestrationRepository)
            runtime = application.state.mysql_orchestration_runtime
            expected_types = (
                StepType.LOAD_CALENDAR_OPERATION_DRAFT,
                StepType.VALIDATE_CALENDAR_OPERATION_APPROVAL,
                StepType.EXECUTE_CALENDAR_OPERATION_ITEMS,
                StepType.VERIFY_CALENDAR_OPERATION_RESULTS,
                StepType.FINALIZE_CALENDAR_OPERATION,
            )
            registered = runtime.registry.registered_types()
            assert all(registered.count(item) == 1 for item in expected_types)
            assert application.state.calendar_operation_gateway.provider_name == "none"
            assert (
                get_calendar_operation_run_service(SimpleNamespace(app=application))
                is service
            )
            assert getattr(application.state, "orchestrator_pool", None) is None
            assert not reaper_started
    finally:
        await _cleanup_run_control_plane(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_run_enqueue_response_loss_replay_and_user_isolation(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    """A public Run enqueue persists once and replays after a real API restart."""

    token = uuid4().hex
    env = _mysql_api_environment(mysql_test_url, f"calendar-run-{token}@fitweek.test")
    env["ORCHESTRATOR_ENABLED"] = "true"
    first: ApiProcess | None = None
    second: ApiProcess | None = None
    third: ApiProcess | None = None
    user_id: UUID | None = None
    other_user_id: UUID | None = None
    try:
        first = await start_api_process(env)
        async with AsyncClient(base_url=first.base_url, timeout=10) as client:
            user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
            plan = await _confirmed_plan(client, token)
            root = str(plan["root_plan_id"] or plan["id"])
            draft = await client.post(
                f"/api/v1/plans/{root}/revisions/{plan['revision']}/calendar-operation-drafts",
                json=_draft_payload(token, plan),
            )
            assert draft.status_code == 201, draft.text
            approved = await client.post(
                f"/api/v1/calendar-operation-drafts/{draft.json()['id']}/approve",
                json={"expected_version": draft.json()["version"]},
            )
            assert approved.status_code == 200, approved.text
            payload = {
                "client_request_id": f"run-{token}",
                "draft_id": draft.json()["id"],
            }
            created = await client.post("/api/v1/calendar-operation-runs", json=payload)
            assert created.status_code == 202, created.text
            body = created.json()
        await stop_api_process(first)
        first = None
        second = await start_api_process(env)
        async with AsyncClient(base_url=second.base_url, timeout=10) as client:
            replay = await client.post("/api/v1/calendar-operation-runs", json=payload)
            assert replay.status_code == 200, replay.text
            assert replay.json() == body
            run = await client.get(f"/api/v1/calendar-operation-runs/{body['id']}")
            assert run.status_code == 200 and run.json()["status"] == "QUEUED"
            steps = await client.get(
                f"/api/v1/calendar-operation-runs/{body['id']}/steps"
            )
            assert steps.status_code == 200 and len(steps.json()) == 1
            assert steps.json()[0]["status"] == "READY"
            checkpoints = await client.get(
                f"/api/v1/calendar-operation-runs/{body['id']}/checkpoints"
            )
            audit = await client.get(
                f"/api/v1/calendar-operation-runs/{body['id']}/audit"
            )
            assert checkpoints.status_code == 200 and checkpoints.json() == []
            assert audit.status_code == 200
            assert [item["event_type"] for item in audit.json()] == [
                "RUN_CREATED",
                "RUN_STATUS_CHANGED",
                "STEP_CREATED",
            ]
            unchanged = await client.get(
                f"/api/v1/calendar-operation-drafts/{draft.json()['id']}"
            )
            assert unchanged.status_code == 200 and unchanged.json() == approved.json()
            collision_root = root
            collision_draft = await client.post(
                f"/api/v1/plans/{collision_root}/revisions/"
                f"{plan['revision']}/calendar-operation-drafts",
                json=_draft_payload(
                    f"{token}-collision", plan, calendar_id="calendar-collision"
                ),
            )
            assert collision_draft.status_code == 201, collision_draft.text
            collision_approved = await client.post(
                f"/api/v1/calendar-operation-drafts/{collision_draft.json()['id']}/approve",
                json={"expected_version": collision_draft.json()["version"]},
            )
            assert collision_approved.status_code == 200, collision_approved.text
            collision = await client.post(
                "/api/v1/calendar-operation-runs",
                json={
                    "client_request_id": payload["client_request_id"],
                    "draft_id": collision_draft.json()["id"],
                },
            )
            assert collision.status_code == 409, collision.text
            assert (
                await client.get(f"/api/v1/calendar-operation-runs/{body['id']}")
            ).json() == run.json()
            assert (
                await client.get(
                    f"/api/v1/calendar-operation-drafts/{collision_draft.json()['id']}"
                )
            ).json() == collision_approved.json()
        other_env = _mysql_api_environment(
            mysql_test_url, f"calendar-run-other-{token}@fitweek.test"
        )
        other_env["ORCHESTRATOR_ENABLED"] = "true"
        third = await start_api_process(other_env)
        async with AsyncClient(base_url=third.base_url, timeout=10) as other_client:
            other_user = await other_client.get("/api/v1/users/me")
            other_user_id = UUID(other_user.json()["id"])
            for path in (
                f"/api/v1/calendar-operation-runs/{body['id']}",
                f"/api/v1/calendar-operation-runs/{body['id']}/steps",
                f"/api/v1/calendar-operation-runs/{body['id']}/checkpoints",
                f"/api/v1/calendar-operation-runs/{body['id']}/audit",
                f"/api/v1/calendar-operation-drafts/{draft.json()['id']}",
            ):
                assert (await other_client.get(path)).status_code == 404
        async with mysql_test_database.session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CalendarOperationAttemptModel)
                    .where(
                        CalendarOperationAttemptModel.user_id == str(user_id),
                        CalendarOperationAttemptModel.draft_id == draft.json()["id"],
                    )
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CalendarEventBindingModel)
                    .where(
                        CalendarEventBindingModel.user_id == str(user_id),
                        CalendarEventBindingModel.root_plan_id == root,
                    )
                )
                == 0
            )
    finally:
        if first is not None:
            await stop_api_process(first)
        if second is not None:
            await stop_api_process(second)
        if third is not None:
            await stop_api_process(third)
        await _cleanup_run_control_plane(mysql_test_database, user_id)
        await _cleanup_run_control_plane(mysql_test_database, other_user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_run_concurrent_idempotency_and_direct_routes_have_zero_side_effect(  # noqa: E501
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    """Concurrent public enqueues converge while legacy routes cannot execute."""

    token = uuid4().hex
    env = _mysql_api_environment(
        mysql_test_url, f"calendar-run-race-{token}@fitweek.test"
    )
    env["ORCHESTRATOR_ENABLED"] = "true"
    process: ApiProcess | None = None
    user_id: UUID | None = None
    try:
        process = await start_api_process(env)
        async with AsyncClient(base_url=process.base_url, timeout=10) as client:
            user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
            plan = await _confirmed_plan(client, token)
            root = str(plan["root_plan_id"] or plan["id"])
            draft_response = await client.post(
                f"/api/v1/plans/{root}/revisions/{plan['revision']}/calendar-operation-drafts",
                json=_draft_payload(token, plan),
            )
            assert draft_response.status_code == 201, draft_response.text
            draft = draft_response.json()
            approved = await client.post(
                f"/api/v1/calendar-operation-drafts/{draft['id']}/approve",
                json={"expected_version": draft["version"]},
            )
            assert approved.status_code == 200, approved.text
            before = approved.json()
            assert (
                await client.post(
                    f"/api/v1/calendar-operation-drafts/{draft['id']}/execute"
                )
            ).status_code == 409
            assert (
                await client.post(
                    f"/api/v1/calendar-operation-drafts/{draft['id']}/retry"
                )
            ).status_code == 409
            assert (
                await client.get(f"/api/v1/calendar-operation-drafts/{draft['id']}")
            ).json() == before
            payload = {"client_request_id": f"race-{token}", "draft_id": draft["id"]}
            left, right = await __import__("asyncio").gather(
                client.post("/api/v1/calendar-operation-runs", json=payload),
                client.post("/api/v1/calendar-operation-runs", json=payload),
            )
            assert all(item.status_code in {200, 202} for item in (left, right))
            assert left.json() == right.json()
            run_id = left.json()["id"]
        async with mysql_test_database.session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(PlanningRunModel)
                    .where(PlanningRunModel.id == run_id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CheckpointModel)
                    .where(CheckpointModel.run_id == run_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEventModel)
                    .where(
                        AuditEventModel.run_id == run_id,
                        AuditEventModel.event_type.not_in(
                            ["RUN_CREATED", "RUN_STATUS_CHANGED", "STEP_CREATED"]
                        ),
                    )
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CalendarOperationAttemptModel)
                    .where(CalendarOperationAttemptModel.user_id == str(user_id))
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CalendarEventBindingModel)
                    .where(CalendarEventBindingModel.user_id == str(user_id))
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AgentStepModel)
                    .where(AgentStepModel.run_id == run_id)
                )
                == 1
            )
    finally:
        if process is not None:
            await stop_api_process(process)
        await _cleanup_run_control_plane(mysql_test_database, user_id)
