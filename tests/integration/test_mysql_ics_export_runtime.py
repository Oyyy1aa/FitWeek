"""MySQL composition and process-boundary coverage for ICS exports."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.api.dependencies import get_ics_export_service, get_tool_gateway
from app.application.ics_export import IcsExportService
from app.config import get_settings
from app.domain.tools.enums import ToolId
from app.persistence.database import Database
from app.persistence.mysql.ics_export_repository import MySQLIcsExportRepository
from app.persistence.mysql.models import IcsExportModel
from app.reliability.fault_injection import FaultInjectionPlan, FaultInjector, FaultType
from tests.integration.test_mysql_schedule_draft_runtime import (
    _cleanup as _cleanup_schedule_data,
)
from tests.integration.test_mysql_schedule_draft_runtime import _schedule_payload
from tests.integration.test_mysql_session_design_draft_runtime import (
    _generation_payload,
)
from tests.support.mysql_orchestrator_process import (
    ApiProcess,
    start_api_process,
    stop_api_process,
)


async def _cleanup(database: Database, user_id: UUID | None) -> None:
    if user_id is None:
        return
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(IcsExportModel).where(IcsExportModel.user_id == str(user_id))
            )
    await _cleanup_schedule_data(database, user_id)


async def _confirmed_plan(
    client: AsyncClient, token: str, *, week_offset_days: int = 0
) -> dict[str, object]:
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
    if week_offset_days:
        payload["week_start"] = (
            date.fromisoformat(str(payload["week_start"]))
            + timedelta(days=week_offset_days)
        ).isoformat()
        payload["availability_slots"] = [
            {
                **slot,
                "start": (
                    datetime.fromisoformat(str(slot["start"]))
                    + timedelta(days=week_offset_days)
                ).isoformat(),
                "end": (
                    datetime.fromisoformat(str(slot["end"]))
                    + timedelta(days=week_offset_days)
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
            "CALENDAR_WRITE_ENABLED": "false",
        }
    )
    return environment


def _unfold_ics_lines(content: bytes) -> list[str]:
    logical: list[str] = []
    for line in content.decode("utf-8").split("\r\n"):
        if not line:
            continue
        if line.startswith(" "):
            logical[-1] += line[1:]
        else:
            logical.append(line)
    return logical


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_ics_dependencies_never_use_memory_and_preserve_typed_tool_gateway(
    monkeypatch: pytest.MonkeyPatch,
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """Durable lifespan dependencies must resolve without BusinessContainer."""

    import app.main as main_module

    factory = getattr(main_module, "build_mysql_ics_export_services", None)
    assert callable(factory), "MySQL ICS composition must use its focused factory"
    calls = 0

    def counted_factory(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return factory(*args, **kwargs)

    email = f"ics-runtime-{uuid4().hex}@fitweek.test"
    monkeypatch.setenv("PERSISTENCE_BACKEND", "mysql")
    monkeypatch.setenv("DATABASE_URL", mysql_test_url)
    monkeypatch.setenv("SINGLE_USER_EMAIL", email)
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "false")
    monkeypatch.setattr(main_module, "build_mysql_ics_export_services", counted_factory)
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    application = main_module.create_application()
    user_id: UUID | None = None
    try:
        async with application.router.lifespan_context(application):
            user_id = application.state.local_user.id
            assert application.state.business_container is None
            assert isinstance(
                application.state.ics_export_repository, MySQLIcsExportRepository
            )
            assert isinstance(application.state.ics_export_service, IcsExportService)
            request = SimpleNamespace(app=application)
            assert (
                get_ics_export_service(request) is application.state.ics_export_service
            )
            assert get_tool_gateway(request) is application.state.ics_tool_gateway
            assert calls == 1
    finally:
        await _cleanup(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_ics_export_survives_real_api_restart_with_exact_deterministic_bytes(  # noqa: E501
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """A persisted export must be downloaded and replayed by a new Uvicorn app."""

    token = uuid4().hex
    environment = _mysql_api_environment(
        mysql_test_url, f"ics-restart-{token}@fitweek.test"
    )
    process_a: ApiProcess | None = None
    process_b: ApiProcess | None = None
    process_c: ApiProcess | None = None
    user_id: UUID | None = None
    other_user_id: UUID | None = None
    export_id = ""
    root_plan_id = ""
    revision = 0
    payload: dict[str, object] = {}
    content = b""
    try:
        process_a = await start_api_process(environment)
        async with AsyncClient(base_url=process_a.base_url, timeout=10) as client:
            user = await client.get("/api/v1/users/me")
            assert user.status_code == 200, user.text
            user_id = UUID(user.json()["id"])
            plan = await _confirmed_plan(client, token)
            root_plan_id = str(plan["root_plan_id"] or plan["id"])
            revision = int(plan["revision"])
            payload = {
                "client_request_id": f"ics-restart-{token}",
                "expected_plan_version": plan["version"],
            }
            created = await client.post(
                f"/api/v1/plans/{root_plan_id}/revisions/{revision}/ics-export",
                json=payload,
            )
            assert created.status_code == 201, created.text
            body = created.json()
            export_id = body["id"]
            downloaded = await client.get(f"/api/v1/ics-exports/{export_id}/download")
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.headers["content-type"].startswith("text/calendar")
            assert downloaded.headers["content-disposition"] == (
                f'attachment; filename="{body["filename"]}"'
            )
            assert body["filename"].isascii()
            assert body["filename"].endswith(".ics")
            assert not any(
                value in body["filename"] for value in ("/", "\\", '"', "\r", "\n")
            )
            assert downloaded.content.startswith(b"BEGIN:VCALENDAR\r\n")
            assert downloaded.content.endswith(b"END:VCALENDAR\r\n")
            assert b"\n" not in downloaded.content.replace(b"\r\n", b"")
            assert len(downloaded.content) == body["byte_size"]
            assert (
                hashlib.sha256(downloaded.content).hexdigest() == body["content_sha256"]
            )
            physical_lines = downloaded.content.split(b"\r\n")[:-1]
            assert all(len(line) <= 75 for line in physical_lines)
            decoded_lines = [line.decode("utf-8") for line in physical_lines]
            assert "TZID" not in "\r\n".join(decoded_lines)
            date_lines = [
                line
                for line in decoded_lines
                if line.startswith(("DTSTART:", "DTEND:"))
            ]
            assert date_lines
            assert all(
                re.fullmatch(r"DT(?:START|END):\d{8}T\d{6}Z", line)
                for line in date_lines
            )
            uids = [
                line.removeprefix("UID:")
                for line in _unfold_ics_lines(downloaded.content)
                if line.startswith("UID:")
            ]
            assert len(uids) == body["event_count"]
            assert len(set(uids)) == len(uids)
            assert all("\r" not in value and "\n" not in value for value in uids)
            assert all(
                re.fullmatch(r"[0-9a-f]{64}@fitweek\.local", value) for value in uids
            )
            content = downloaded.content
            invocations = await client.get("/api/v1/tool-gateway/invocations")
            assert invocations.status_code == 200, invocations.text
            summaries = invocations.json()
            assert [item["tool_id"] for item in summaries].count("ICS_EXPORT") == 1
            assert all(item["tool_id"] != "CALENDAR_COMMIT" for item in summaries)
        await stop_api_process(process_a)
        process_a = None

        process_b = await start_api_process(environment)
        async with AsyncClient(base_url=process_b.base_url, timeout=10) as client:
            metadata = await client.get(f"/api/v1/ics-exports/{export_id}")
            assert metadata.status_code == 200, metadata.text
            replay = await client.post(
                f"/api/v1/plans/{root_plan_id}/revisions/{revision}/ics-export",
                json=payload,
            )
            assert replay.status_code == 200, replay.text
            replay_body = replay.json()
            assert replay_body["id"] == export_id
            for key in (
                "client_request_id",
                "request_fingerprint",
                "root_plan_id",
                "revision",
                "plan_version",
                "policy_version",
                "content_sha256",
                "event_count",
                "byte_size",
                "filename",
                "created_at",
            ):
                assert metadata.json()[key] == body[key] == replay_body[key]
            repeated_download = await client.get(
                f"/api/v1/ics-exports/{export_id}/download"
            )
            assert repeated_download.status_code == 200, repeated_download.text
            assert repeated_download.content == content
            assert (
                hashlib.sha256(repeated_download.content).hexdigest()
                == replay_body["content_sha256"]
            )
            after_replay = await client.get("/api/v1/tool-gateway/invocations")
            assert after_replay.status_code == 200
            assert not [
                item for item in after_replay.json() if item["tool_id"] == "ICS_EXPORT"
            ]
        process_c = await start_api_process(
            _mysql_api_environment(
                mysql_test_url, f"ics-isolation-{token}@fitweek.test"
            )
        )
        async with AsyncClient(base_url=process_c.base_url, timeout=10) as client:
            other = await client.get("/api/v1/users/me")
            assert other.status_code == 200
            other_user_id = UUID(other.json()["id"])
            assert (
                await client.get(f"/api/v1/ics-exports/{export_id}")
            ).status_code == 404
            assert (
                await client.get(f"/api/v1/ics-exports/{export_id}/download")
            ).status_code == 404
    finally:
        if process_a is not None:
            await stop_api_process(process_a)
        if process_b is not None:
            await stop_api_process(process_b)
        if process_c is not None:
            await stop_api_process(process_c)
        await _cleanup(mysql_test_database, user_id)
        await _cleanup(mysql_test_database, other_user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_ics_response_loss_and_concurrent_requests_keep_one_artifact(
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """Concurrent public POSTs converge on the MySQL request uniqueness fact."""

    token = uuid4().hex
    process: ApiProcess | None = None
    user_id: UUID | None = None
    try:
        process = await start_api_process(
            _mysql_api_environment(
                mysql_test_url, f"ics-concurrent-{token}@fitweek.test"
            )
        )
        async with AsyncClient(base_url=process.base_url, timeout=10) as client:
            user = await client.get("/api/v1/users/me")
            assert user.status_code == 200
            user_id = UUID(user.json()["id"])
            plan = await _confirmed_plan(client, token)
            root = str(plan["root_plan_id"] or plan["id"])
            revision = int(plan["revision"])
            payload = {
                "client_request_id": f"ics-concurrent-{token}",
                "expected_plan_version": plan["version"],
            }
            first, second = await asyncio.gather(
                client.post(
                    f"/api/v1/plans/{root}/revisions/{revision}/ics-export",
                    json=payload,
                ),
                client.post(
                    f"/api/v1/plans/{root}/revisions/{revision}/ics-export",
                    json=payload,
                ),
            )
            assert first.status_code in {200, 201}, first.text
            assert second.status_code in {200, 201}, second.text
            first_body, second_body = first.json(), second.json()
            for key in (
                "id",
                "request_fingerprint",
                "content_sha256",
                "event_count",
                "byte_size",
                "filename",
            ):
                assert first_body[key] == second_body[key]
            download = await client.get(
                f"/api/v1/ics-exports/{first_body['id']}/download"
            )
            assert download.status_code == 200, download.text
            assert len(download.content) == first_body["byte_size"]
            assert (
                hashlib.sha256(download.content).hexdigest()
                == first_body["content_sha256"]
            )
            async with mysql_test_database.session_factory() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(IcsExportModel)
                    .where(
                        IcsExportModel.user_id == str(user_id),
                        IcsExportModel.client_request_id
                        == payload["client_request_id"],
                    )
                )
            assert count == 1
            replay = await client.post(
                f"/api/v1/plans/{root}/revisions/{revision}/ics-export",
                json=payload,
            )
            assert replay.status_code == 200, replay.text
            assert replay.json() == first_body
            changed_plan = await _confirmed_plan(
                client, f"{token}-changed", week_offset_days=7
            )
            changed_root = str(changed_plan["root_plan_id"] or changed_plan["id"])
            conflict = await client.post(
                f"/api/v1/plans/{changed_root}/revisions/{changed_plan['revision']}/ics-export",
                json={
                    "client_request_id": payload["client_request_id"],
                    "expected_plan_version": changed_plan["version"],
                },
            )
            assert conflict.status_code == 409, conflict.text
    finally:
        if process is not None:
            await stop_api_process(process)
        await _cleanup(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_ics_rejects_invalid_revision_serialization_failure_and_calendar_side_effects(  # noqa: E501
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unconfirmed revisions cannot create an artifact or a Calendar effect."""

    token = uuid4().hex
    process: ApiProcess | None = None
    user_id: UUID | None = None
    failed_user_id: UUID | None = None
    try:
        process = await start_api_process(
            _mysql_api_environment(mysql_test_url, f"ics-invalid-{token}@fitweek.test")
        )
        async with AsyncClient(base_url=process.base_url, timeout=10) as client:
            user = await client.get("/api/v1/users/me")
            assert user.status_code == 200
            user_id = UUID(user.json()["id"])
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
            assert profile.status_code == 200
            generated = await client.post(
                "/api/v1/plans/generate", json=_generation_payload(token)
            )
            assert generated.status_code == 201
            plan = generated.json()["plan"]
            root = str(plan["root_plan_id"] or plan["id"])
            rejected = await client.post(
                f"/api/v1/plans/{root}/revisions/{plan['revision']}/ics-export",
                json={
                    "client_request_id": f"ics-invalid-{token}",
                    "expected_plan_version": plan["version"],
                },
            )
            assert rejected.status_code == 409, rejected.text
            async with mysql_test_database.session_factory() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(IcsExportModel)
                    .where(IcsExportModel.user_id == str(user_id))
                )
            assert count == 0
            invocations = await client.get("/api/v1/tool-gateway/invocations")
            assert invocations.status_code == 200
            assert not [
                item
                for item in invocations.json()
                if item["tool_id"] in {"ICS_EXPORT", "CALENDAR_COMMIT"}
            ]
        await stop_api_process(process)
        process = None

        import app.main as main_module

        email = f"ics-serialization-{token}@fitweek.test"
        monkeypatch.setenv("PERSISTENCE_BACKEND", "mysql")
        monkeypatch.setenv("DATABASE_URL", mysql_test_url)
        monkeypatch.setenv("SINGLE_USER_EMAIL", email)
        monkeypatch.setenv("REDIS_ENABLED", "false")
        monkeypatch.setenv("ORCHESTRATOR_ENABLED", "false")
        monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
        get_settings.cache_clear()
        application = main_module.create_application()
        async with application.router.lifespan_context(application):
            failed_user_id = application.state.local_user.id
            application.state.ics_tool_gateway.fault_injector = FaultInjector(
                (
                    FaultInjectionPlan(
                        fault_type=FaultType.INVALID_RESPONSE,
                        tool_id=ToolId.ICS_EXPORT,
                        fail_attempts=frozenset({1}),
                    ),
                )
            )
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                failed_plan = await _confirmed_plan(client, f"{token}-serialization")
                failed_root = str(failed_plan["root_plan_id"] or failed_plan["id"])
                schedule = await client.post(
                    "/api/v1/schedule-drafts",
                    json=_schedule_payload(
                        f"ics-old-revision-{token}",
                        failed_plan,
                        failed_plan["sessions"][0],
                    ),
                )
                assert schedule.status_code == 201, schedule.text
                accepted = await client.post(
                    f"/api/v1/schedule-drafts/{schedule.json()['id']}/accept",
                    json={"expected_version": schedule.json()["version"]},
                )
                assert accepted.status_code == 200, accepted.text
                applied = await client.post(
                    f"/api/v1/schedule-drafts/{schedule.json()['id']}/apply",
                    json={
                        "client_request_id": f"ics-old-revision-apply-{token}",
                        "expected_draft_version": accepted.json()["version"],
                        "root_plan_id": failed_root,
                        "source_revision": failed_plan["revision"],
                        "expected_plan_version": failed_plan["version"],
                    },
                )
                assert applied.status_code == 201, applied.text
                later = applied.json()["plan"]
                confirmed_later = await client.post(
                    f"/api/v1/plans/{failed_root}/revisions/{later['revision']}/confirm",
                    json={"expected_version": later["version"]},
                )
                assert confirmed_later.status_code == 200, confirmed_later.text
                current_later = confirmed_later.json()["plan"]
                old_rejected = await client.post(
                    f"/api/v1/plans/{failed_root}/revisions/{failed_plan['revision']}/ics-export",
                    json={
                        "client_request_id": f"ics-old-revision-{token}",
                        "expected_plan_version": failed_plan["version"],
                    },
                )
                assert old_rejected.status_code == 409, old_rejected.text
                async with mysql_test_database.session_factory() as session:
                    old_count = await session.scalar(
                        select(func.count())
                        .select_from(IcsExportModel)
                        .where(IcsExportModel.user_id == str(failed_user_id))
                    )
                assert old_count == 0
                serialization = await client.post(
                    f"/api/v1/plans/{failed_root}/revisions/{later['revision']}/ics-export",
                    json={
                        "client_request_id": f"ics-serialization-{token}",
                        "expected_plan_version": current_later["version"],
                    },
                )
                assert serialization.status_code == 422, serialization.text
                assert (
                    serialization.json()["error"]["code"] == "ICS_SERIALIZATION_FAILED"
                )
                async with mysql_test_database.session_factory() as session:
                    count = await session.scalar(
                        select(func.count())
                        .select_from(IcsExportModel)
                        .where(IcsExportModel.user_id == str(failed_user_id))
                    )
                assert count == 0
    finally:
        if process is not None:
            await stop_api_process(process)
        await _cleanup(mysql_test_database, user_id)
        await _cleanup(mysql_test_database, failed_user_id)
