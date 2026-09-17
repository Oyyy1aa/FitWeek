"""MySQL Calendar Draft/Review composition and real API restart coverage."""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import delete, func, select

import app.main as main_module
from app.api.dependencies import get_calendar_operation_service
from app.application.calendar_operations import CalendarOperationService
from app.calendar_operations.gateway import CalendarWriteGateway
from app.persistence.database import Database
from app.persistence.mysql.calendar_operation_repository import (
    MySQLCalendarOperationRepository,
)
from app.persistence.mysql.models import (
    CalendarEventBindingModel,
    CalendarOperationAttemptModel,
    CalendarOperationDraftModel,
    CalendarOperationItemModel,
)
from tests.integration.test_mysql_schedule_draft_runtime import (
    _cleanup as _cleanup_schedule_data,
)
from tests.integration.test_mysql_session_design_draft_runtime import (
    _generation_payload,
)
from tests.support.mysql_orchestrator_process import (
    ApiProcess,
    start_api_process,
    stop_api_process,
)


async def _cleanup(database: Database, user_id: UUID | None) -> None:
    """Remove only the generated user and its dependent Calendar/Plan facts."""

    if user_id is None:
        return
    owner = str(user_id)
    async with database.session_factory() as session:
        async with session.begin():
            draft_ids = (
                await session.scalars(
                    select(CalendarOperationDraftModel.id).where(
                        CalendarOperationDraftModel.user_id == owner
                    )
                )
            ).all()
            if draft_ids:
                item_ids = (
                    await session.scalars(
                        select(CalendarOperationItemModel.id).where(
                            CalendarOperationItemModel.draft_id.in_(draft_ids)
                        )
                    )
                ).all()
                if item_ids:
                    await session.execute(
                        delete(CalendarOperationAttemptModel).where(
                            CalendarOperationAttemptModel.item_id.in_(item_ids)
                        )
                    )
                await session.execute(
                    delete(CalendarOperationItemModel).where(
                        CalendarOperationItemModel.draft_id.in_(draft_ids)
                    )
                )
                await session.execute(
                    delete(CalendarOperationAttemptModel).where(
                        CalendarOperationAttemptModel.draft_id.in_(draft_ids)
                    )
                )
            await session.execute(
                delete(CalendarOperationDraftModel).where(
                    CalendarOperationDraftModel.user_id == owner
                )
            )
            await session.execute(
                delete(CalendarEventBindingModel).where(
                    CalendarEventBindingModel.user_id == owner
                )
            )
    await _cleanup_schedule_data(database, user_id)


def _mysql_api_environment(database_url: str, email: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "PERSISTENCE_BACKEND": "mysql",
            "DATABASE_URL": database_url,
            "SINGLE_USER_EMAIL": email,
            "REDIS_ENABLED": "false",
            "ORCHESTRATOR_ENABLED": "false",
            "MODEL_GATEWAY_ENABLED": "false",
            "CALENDAR_READ_ENABLED": "false",
            # The MySQL Calendar review factory must not use this setting to
            # construct a provider or enable any write path.
            "CALENDAR_WRITE_ENABLED": "true",
        }
    )
    return environment


def _sanitized_response_failure(response: Response) -> dict[str, int | str]:
    """Expose bounded response categories without copying payload or internals."""

    try:
        body = response.json()
    except ValueError:
        detail: object = None
        detail_kind = "non-json"
        detail_length = min(len(response.content), 256)
    else:
        detail = body.get("detail") if isinstance(body, dict) else body
        detail_kind = type(detail).__name__
        detail_length = min(len(str(detail)), 256)
    if response.status_code >= 500:
        category = "server_error"
    elif response.status_code == 409:
        category = "conflict"
    elif response.status_code >= 400:
        category = "client_error"
    else:
        category = "accepted"
    return {
        "status_code": response.status_code,
        "category": category,
        "detail_kind": detail_kind,
        "detail_length": detail_length,
    }


async def _confirmed_plan(client: AsyncClient, token: str) -> dict[str, object]:
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
    payload = _generation_payload(token)
    payload["week_start"] = (
        date.fromisoformat(str(payload["week_start"])) + timedelta(days=14)
    ).isoformat()
    payload["availability_slots"] = [
        {
            **slot,
            "start": (
                datetime.fromisoformat(str(slot["start"])) + timedelta(days=14)
            ).isoformat(),
            "end": (
                datetime.fromisoformat(str(slot["end"])) + timedelta(days=14)
            ).isoformat(),
        }
        for slot in payload["availability_slots"]
    ]
    generated = await client.post("/api/v1/plans/generate", json=payload)
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed = await client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _draft_payload(
    token: str, plan: dict[str, object], *, calendar_id: str = "calendar-review"
) -> dict[str, object]:
    return {
        "client_request_id": f"calendar-review-{token}",
        "expected_plan_version": plan["version"],
        "provider": "test-calendar",
        "calendar_id": calendar_id,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_startup_failure_closes_gateway_once_and_clears_typed_state(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """A post-Calendar startup fault must clean the real typed state once."""

    sentinel = RuntimeError("calendar-startup-sentinel")
    email = f"calendar-startup-{uuid4().hex}@fitweek.test"
    monkeypatch.setenv("PERSISTENCE_BACKEND", "mysql")
    monkeypatch.setenv("DATABASE_URL", mysql_test_url)
    monkeypatch.setenv("SINGLE_USER_EMAIL", email)
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "false")
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)

    def raise_after_calendar(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise sentinel

    close_calls: list[CalendarWriteGateway] = []
    actual_close = CalendarWriteGateway.close

    async def observe_close(gateway: CalendarWriteGateway) -> None:
        close_calls.append(gateway)
        await actual_close(gateway)

    monkeypatch.setattr(
        main_module, "build_mysql_memory_services", raise_after_calendar
    )
    monkeypatch.setattr(CalendarWriteGateway, "close", observe_close)
    application = main_module.create_application()
    user_id: UUID | None = None
    try:
        with pytest.raises(RuntimeError) as raised:
            async with application.router.lifespan_context(application):
                pytest.fail("startup sentinel must prevent yield")
        assert raised.value is sentinel
        user = getattr(application.state, "local_user", None)
        user_id = None if user is None else user.id
        assert len(close_calls) == 1
        assert application.state.calendar_operation_repository is None
        assert application.state.calendar_operation_gateway is None
        assert application.state.calendar_operation_service is None
    finally:
        await _cleanup(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_dependencies_use_durable_repository_and_api_gateway_is_always_disabled(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """MySQL Calendar Draft dependencies must never resolve through memory state."""

    email = f"calendar-runtime-{uuid4().hex}@fitweek.test"
    monkeypatch.setenv("PERSISTENCE_BACKEND", "mysql")
    monkeypatch.setenv("DATABASE_URL", mysql_test_url)
    monkeypatch.setenv("SINGLE_USER_EMAIL", email)
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "false")
    # This deliberately proves the MySQL factory is non-writing even when a
    # deployment configuration tries to enable Calendar writes.
    monkeypatch.setenv("CALENDAR_WRITE_ENABLED", "true")
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    application = main_module.create_application()
    user_id: UUID | None = None
    process: ApiProcess | None = None
    process_user_id: UUID | None = None
    try:
        async with application.router.lifespan_context(application):
            user_id = application.state.local_user.id
            assert application.state.business_container is None
            assert isinstance(
                application.state.calendar_operation_repository,
                MySQLCalendarOperationRepository,
            )
            assert isinstance(
                application.state.calendar_operation_gateway,
                CalendarWriteGateway,
            )
            assert application.state.calendar_operation_gateway.provider_name == "none"
            assert isinstance(
                application.state.calendar_operation_service,
                CalendarOperationService,
            )
            request = SimpleNamespace(app=application)
            assert (
                get_calendar_operation_service(request)
                is application.state.calendar_operation_service
            )
        process = await start_api_process(
            _mysql_api_environment(
                mysql_test_url, f"calendar-http-{uuid4().hex}@fitweek.test"
            )
        )
        async with AsyncClient(base_url=process.base_url, timeout=10) as client:
            user = await client.get("/api/v1/users/me")
            assert user.status_code == 200, user.text
            process_user_id = UUID(user.json()["id"])
            missing = await client.get(f"/api/v1/calendar-operation-drafts/{uuid4()}")
            assert missing.status_code == 404, missing.text
    finally:
        if process is not None:
            await stop_api_process(process)
        await _cleanup(mysql_test_database, user_id)
        await _cleanup(mysql_test_database, process_user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_draft_response_loss_replay_and_review_survive_real_api_restarts(  # noqa: E501
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """Draft replay and terminal review facts survive independently restarted APIs."""

    token = uuid4().hex
    environment = _mysql_api_environment(
        mysql_test_url, f"calendar-restart-{token}@fitweek.test"
    )
    process_a: ApiProcess | None = None
    process_b: ApiProcess | None = None
    process_c: ApiProcess | None = None
    process_d: ApiProcess | None = None
    user_id: UUID | None = None
    other_user_id: UUID | None = None
    approved_id = ""
    rejected_id = ""
    try:
        process_a = await start_api_process(environment)
        async with AsyncClient(base_url=process_a.base_url, timeout=10) as client:
            user = await client.get("/api/v1/users/me")
            assert user.status_code == 200, user.text
            user_id = UUID(user.json()["id"])
            plan = await _confirmed_plan(client, token)
            root_plan_id = str(plan["root_plan_id"] or plan["id"])
            revision = int(plan["revision"])
            payload = _draft_payload(token, plan)
            created = await client.post(
                f"/api/v1/plans/{root_plan_id}/revisions/{revision}/calendar-operation-drafts",
                json=payload,
            )
            assert created.status_code == 201, created.text
            created_body = created.json()
            approved_id = created_body["id"]
        await stop_api_process(process_a)
        process_a = None

        process_b = await start_api_process(environment)
        async with AsyncClient(base_url=process_b.base_url, timeout=10) as client:
            replay = await client.post(
                f"/api/v1/plans/{root_plan_id}/revisions/{revision}/calendar-operation-drafts",
                json=payload,
            )
            assert replay.status_code == 200, replay.text
            assert replay.json() == created_body
            restored = await client.get(
                f"/api/v1/calendar-operation-drafts/{approved_id}"
            )
            assert restored.status_code == 200, restored.text
            assert restored.json() == created_body
            approved = await client.post(
                f"/api/v1/calendar-operation-drafts/{approved_id}/approve",
                json={"expected_version": created_body["version"]},
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "APPROVED"
            second_payload = _draft_payload(
                f"{token}-reject", plan, calendar_id="calendar-review-reject"
            )
            rejected = await client.post(
                f"/api/v1/plans/{root_plan_id}/revisions/{revision}/calendar-operation-drafts",
                json=second_payload,
            )
            assert rejected.status_code == 201, rejected.text
            rejected_id = rejected.json()["id"]
            rejected_review = await client.post(
                f"/api/v1/calendar-operation-drafts/{rejected_id}/reject",
                json={"expected_version": rejected.json()["version"]},
            )
            assert rejected_review.status_code == 200, rejected_review.text
            assert rejected_review.json()["status"] == "REJECTED"
        await stop_api_process(process_b)
        process_b = None

        process_c = await start_api_process(environment)
        async with AsyncClient(base_url=process_c.base_url, timeout=10) as client:
            assert (
                await client.get(f"/api/v1/calendar-operation-drafts/{approved_id}")
            ).json()["status"] == "APPROVED"
            assert (
                await client.get(f"/api/v1/calendar-operation-drafts/{rejected_id}")
            ).json()["status"] == "REJECTED"
        process_d = await start_api_process(
            _mysql_api_environment(
                mysql_test_url, f"calendar-isolation-{token}@fitweek.test"
            )
        )
        async with AsyncClient(base_url=process_d.base_url, timeout=10) as client:
            other = await client.get("/api/v1/users/me")
            assert other.status_code == 200, other.text
            other_user_id = UUID(other.json()["id"])
            assert (
                await client.get(f"/api/v1/calendar-operation-drafts/{approved_id}")
            ).status_code == 404
    finally:
        for process in (process_a, process_b, process_c, process_d):
            if process is not None:
                await stop_api_process(process)
        await _cleanup(mysql_test_database, user_id)
        await _cleanup(mysql_test_database, other_user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_concurrent_create_review_conflict_and_user_isolation(
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """Public concurrent create/review calls converge on the MySQL facts."""

    token = uuid4().hex
    process: ApiProcess | None = None
    other_process: ApiProcess | None = None
    user_id: UUID | None = None
    other_user_id: UUID | None = None
    try:
        process = await start_api_process(
            _mysql_api_environment(
                mysql_test_url, f"calendar-race-{token}@fitweek.test"
            )
        )
        async with AsyncClient(base_url=process.base_url, timeout=10) as client:
            user = await client.get("/api/v1/users/me")
            assert user.status_code == 200, user.text
            user_id = UUID(user.json()["id"])
            plan = await _confirmed_plan(client, token)
            root_plan_id = str(plan["root_plan_id"] or plan["id"])
            revision = int(plan["revision"])
            payload = _draft_payload(token, plan)
            endpoint = (
                f"/api/v1/plans/{root_plan_id}/revisions/{revision}/"
                "calendar-operation-drafts"
            )
            first, second = await asyncio.gather(
                client.post(endpoint, json=payload), client.post(endpoint, json=payload)
            )
            create_responses = (first, second)
            assert all(
                response.status_code in {200, 201} for response in create_responses
            ), [
                _sanitized_response_failure(response)
                for response in create_responses
                if response.status_code not in {200, 201}
            ]
            first_body, second_body = first.json(), second.json()
            assert first_body == second_body
            replay = await client.post(endpoint, json=payload)
            assert replay.status_code in {200, 201}, _sanitized_response_failure(replay)
            assert replay.json() == first_body
            draft_id = first_body["id"]
            keys = [item["operation_key"] for item in first_body["items"]]
            assert all(len(value) == 64 for value in keys)
            approve_endpoint = f"/api/v1/calendar-operation-drafts/{draft_id}/approve"
            left, right = await asyncio.gather(
                client.post(
                    approve_endpoint, json={"expected_version": first_body["version"]}
                ),
                client.post(
                    approve_endpoint, json={"expected_version": first_body["version"]}
                ),
            )
            assert sorted((left.status_code, right.status_code)) == [200, 409]
            initial_version = first_body["version"]
            winner = left if left.status_code == 200 else right
            assert winner.json()["version"] == initial_version + 1
            durable_approved = await client.get(
                f"/api/v1/calendar-operation-drafts/{draft_id}"
            )
            assert durable_approved.status_code == 200, durable_approved.text
            approved_body = durable_approved.json()
            assert approved_body["status"] == "APPROVED"
            assert approved_body["version"] == initial_version + 1
            stale_reject = await client.post(
                f"/api/v1/calendar-operation-drafts/{draft_id}/reject",
                json={"expected_version": initial_version},
            )
            assert stale_reject.status_code == 409, stale_reject.text
            after_stale_reject = await client.get(
                f"/api/v1/calendar-operation-drafts/{draft_id}"
            )
            assert after_stale_reject.status_code == 200, after_stale_reject.text
            assert after_stale_reject.json() == approved_body
            different_input = await client.post(
                endpoint,
                json={**payload, "calendar_id": "calendar-conflict"},
            )
            assert different_input.status_code == 409, _sanitized_response_failure(
                different_input
            )
            after_different_input = await client.get(
                f"/api/v1/calendar-operation-drafts/{draft_id}"
            )
            assert after_different_input.status_code == 200
            assert after_different_input.json() == approved_body
        async with mysql_test_database.session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CalendarOperationDraftModel)
                    .where(CalendarOperationDraftModel.id == draft_id)
                )
                == 1
            )
            assert await session.scalar(
                select(func.count())
                .select_from(CalendarOperationItemModel)
                .where(CalendarOperationItemModel.draft_id == draft_id)
            ) == len(first_body["items"])
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CalendarOperationAttemptModel)
                    .where(CalendarOperationAttemptModel.draft_id == draft_id)
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
            sequence_numbers = (
                await session.scalars(
                    select(CalendarOperationItemModel.sequence_no)
                    .where(CalendarOperationItemModel.draft_id == draft_id)
                    .order_by(CalendarOperationItemModel.sequence_no)
                )
            ).all()
            assert sequence_numbers == list(range(1, len(first_body["items"]) + 1))
        other_process = await start_api_process(
            _mysql_api_environment(
                mysql_test_url, f"calendar-race-other-{token}@fitweek.test"
            )
        )
        async with AsyncClient(base_url=other_process.base_url, timeout=10) as client:
            other = await client.get("/api/v1/users/me")
            assert other.status_code == 200, other.text
            other_user_id = UUID(other.json()["id"])
            assert (
                await client.get(f"/api/v1/calendar-operation-drafts/{draft_id}")
            ).status_code == 404
    finally:
        if process is not None:
            await stop_api_process(process)
        if other_process is not None:
            await stop_api_process(other_process)
        await _cleanup(mysql_test_database, user_id)
        await _cleanup(mysql_test_database, other_user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_calendar_direct_execute_retry_cannot_write_binding_attempt_or_item_state(  # noqa: E501
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """The API-side MySQL gateway cannot make a Calendar write even if enabled."""

    token = uuid4().hex
    process: ApiProcess | None = None
    user_id: UUID | None = None
    try:
        process = await start_api_process(
            _mysql_api_environment(
                mysql_test_url, f"calendar-deny-{token}@fitweek.test"
            )
        )
        async with AsyncClient(base_url=process.base_url, timeout=10) as client:
            user = await client.get("/api/v1/users/me")
            assert user.status_code == 200, user.text
            user_id = UUID(user.json()["id"])
            plan = await _confirmed_plan(client, token)
            root_plan_id = str(plan["root_plan_id"] or plan["id"])
            revision = int(plan["revision"])
            created = await client.post(
                f"/api/v1/plans/{root_plan_id}/revisions/{revision}/calendar-operation-drafts",
                json=_draft_payload(token, plan),
            )
            assert created.status_code == 201, created.text
            draft_id = created.json()["id"]
            approved = await client.post(
                f"/api/v1/calendar-operation-drafts/{draft_id}/approve",
                json={"expected_version": created.json()["version"]},
            )
            assert approved.status_code == 200, approved.text
            before_statuses = [item["status"] for item in approved.json()["items"]]
            assert (
                await client.post(
                    f"/api/v1/calendar-operation-drafts/{draft_id}/execute"
                )
            ).status_code == 409
            assert (
                await client.post(f"/api/v1/calendar-operation-drafts/{draft_id}/retry")
            ).status_code == 409
            after = await client.get(f"/api/v1/calendar-operation-drafts/{draft_id}")
            assert after.status_code == 200, after.text
            assert [item["status"] for item in after.json()["items"]] == before_statuses
        async with mysql_test_database.session_factory() as session:
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
                    .select_from(CalendarOperationAttemptModel)
                    .where(CalendarOperationAttemptModel.user_id == str(user_id))
                )
                == 0
            )
    finally:
        if process is not None:
            await stop_api_process(process)
        await _cleanup(mysql_test_database, user_id)
