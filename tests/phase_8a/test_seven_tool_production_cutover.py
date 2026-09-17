"""Seven production entry points prove the Phase 8A Tool cutover."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import BusinessContainer
from app.config import get_settings
from app.domain.tools.enums import ToolCaller, ToolId
from app.main import create_application
from tests.api.helpers import profile_payload
from tests.phase_8a.test_remaining_tool_cutovers import (
    _adapter,
    _candidate_payload,
    _confirmed_plan,
    _profile,
    _recovery_payload,
    _session_payload,
    _traces,
)

pytestmark = pytest.mark.phase_8a


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_application()) as value:
        yield value
    get_settings.cache_clear()


@pytest.fixture
def calendar_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "false")
    monkeypatch.setenv("CALENDAR_READ_ENABLED", "true")
    monkeypatch.setenv("CALENDAR_READ_PROVIDER", "scripted")
    monkeypatch.setenv("CALENDAR_WRITE_ENABLED", "true")
    monkeypatch.setenv("CALENDAR_WRITE_PROVIDER", "scripted")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "true")
    get_settings.cache_clear()
    with TestClient(create_application()) as value:
        yield value
    get_settings.cache_clear()


def _future_generation_payload() -> dict[str, object]:
    today = datetime.now(UTC).date()
    week_start = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return {
        "week_start": week_start.isoformat(),
        "availability_slots": [
            {
                "start": datetime.combine(
                    week_start + timedelta(days=offset),
                    datetime.min.time(),
                    tzinfo=UTC,
                )
                .replace(hour=10)
                .isoformat(),
                "end": datetime.combine(
                    week_start + timedelta(days=offset),
                    datetime.min.time(),
                    tzinfo=UTC,
                )
                .replace(hour=11)
                .isoformat(),
                "location_type": "HOME",
            }
            for offset in (0, 2)
        ],
        "preferred_locations": ["HOME"],
        "preferred_session_types": [],
    }


def _future_plan(value: TestClient) -> dict[str, Any]:
    assert value.put("/api/v1/profiles/me", json=profile_payload()).status_code == 200
    generated = value.post("/api/v1/plans/generate", json=_future_generation_payload())
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed = value.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _schedule_payload(plan: dict[str, Any], request_id: str) -> dict[str, object]:
    windows = []
    for session in plan["sessions"]:
        start = datetime.fromisoformat(
            session["scheduled_start"].replace("Z", "+00:00")
        ) + timedelta(hours=2)
        windows.append(
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=2)).isoformat(),
                "location": "HOME",
            }
        )
    return {
        "client_request_id": request_id,
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "timezone": "UTC",
        "availability_windows": windows,
        "manual_busy_windows": [],
    }


def _calendar_container(value: TestClient) -> BusinessContainer:
    container = value.app.state.business_container
    assert isinstance(container, BusinessContainer)
    return container


def _wait_calendar_run(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/calendar-operation-runs/{run_id}")
        assert response.status_code == 200, response.text
        if response.json()["status"] in {"COMPLETED", "FAILED_PERMANENT"}:
            return response.json()
        time.sleep(0.05)
    pytest.fail("Calendar Run did not reach terminal state.")


def test_calendar_free_busy_production_entry_uses_tool_gateway_once(
    calendar_client: TestClient,
) -> None:
    plan = _future_plan(calendar_client)
    container = _calendar_container(calendar_client)
    adapter = container.tool_gateway.registry.get(
        ToolId.CALENDAR_FREE_BUSY.value, "phase-8a-v1"
    ).adapter
    before = adapter.invocation_count
    created = calendar_client.post(
        "/api/v1/schedule-drafts",
        json=_schedule_payload(plan, "seven-tool-calendar-read"),
    )
    assert created.status_code == 201, created.text
    assert adapter.invocation_count - before == 1
    trace = container.tool_gateway.traces.list_for_user(container.development_user.id)[
        -1
    ]
    assert (trace.tool_id, trace.tool_version, trace.caller) == (
        ToolId.CALENDAR_FREE_BUSY,
        "phase-8a-v1",
        ToolCaller.SCHEDULE_APPLICATION.value,
    )
    assert created.json()["calendar_verification_status"] == "VERIFIED"


def test_calendar_commit_production_entry_uses_tool_gateway_per_item(
    calendar_client: TestClient,
) -> None:
    plan = _future_plan(calendar_client)
    root = plan["root_plan_id"] or plan["id"]
    container = _calendar_container(calendar_client)
    adapter = container.tool_gateway.registry.get(
        ToolId.CALENDAR_COMMIT.value, "phase-8a-v1"
    ).adapter
    before = adapter.invocation_count
    created = calendar_client.post(
        f"/api/v1/plans/{root}/revisions/1/calendar-operation-drafts",
        json={
            "client_request_id": "seven-tool-calendar-write",
            "expected_plan_version": plan["version"],
            "provider": "scripted",
            "calendar_id": "primary",
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    approved = calendar_client.post(
        f"/api/v1/calendar-operation-drafts/{draft['id']}/approve",
        json={"expected_version": draft["version"]},
    )
    assert approved.status_code == 200, approved.text
    direct = calendar_client.post(
        f"/api/v1/calendar-operation-drafts/{draft['id']}/execute"
    )
    assert direct.status_code == 409, direct.text
    retry = calendar_client.post(
        f"/api/v1/calendar-operation-drafts/{draft['id']}/retry"
    )
    assert retry.status_code == 409, retry.text
    assert adapter.invocation_count == before
    run = calendar_client.post(
        "/api/v1/calendar-operation-runs",
        json={
            "client_request_id": "seven-tool-calendar-write-run",
            "draft_id": draft["id"],
        },
    )
    assert run.status_code == 202, run.text
    completed = _wait_calendar_run(calendar_client, run.json()["id"])
    assert completed["status"] == "COMPLETED"
    executed = calendar_client.get(f"/api/v1/calendar-operation-drafts/{draft['id']}")
    assert executed.status_code == 200, executed.text
    assert adapter.invocation_count - before == len(draft["items"])
    traces = tuple(
        item
        for item in container.tool_gateway.traces.list_for_user(
            container.development_user.id
        )
        if item.tool_id is ToolId.CALENDAR_COMMIT
    )
    assert traces and all(
        item.caller == ToolCaller.CALENDAR_EXECUTOR.value
        and item.tool_version == "phase-8a-v1"
        for item in traces
    )
    assert executed.json()["status"] == "SUCCEEDED"


def test_ics_export_production_entry_preserves_deterministic_bytes(
    calendar_client: TestClient,
) -> None:
    plan = _future_plan(calendar_client)
    root = plan["root_plan_id"] or plan["id"]
    container = _calendar_container(calendar_client)
    adapter = container.tool_gateway.registry.get(
        ToolId.ICS_EXPORT.value, "phase-8a-v1"
    ).adapter
    before = adapter.invocation_count
    exported = calendar_client.post(
        f"/api/v1/plans/{root}/revisions/1/ics-export",
        json={
            "client_request_id": "seven-tool-ics",
            "expected_plan_version": plan["version"],
        },
    )
    assert exported.status_code == 201, exported.text
    assert adapter.invocation_count - before == 1
    downloaded = calendar_client.get(
        f"/api/v1/ics-exports/{exported.json()['id']}/download"
    )
    assert downloaded.content.startswith(b"BEGIN:VCALENDAR\r\n")
    assert downloaded.content.endswith(b"END:VCALENDAR\r\n")
    trace = tuple(
        item
        for item in container.tool_gateway.traces.list_for_user(
            container.development_user.id
        )
        if item.tool_id is ToolId.ICS_EXPORT
    )[-1]
    assert (trace.caller, trace.tool_version) == (
        ToolCaller.ICS_EXPORT_SERVICE.value,
        "phase-8a-v1",
    )


def test_catalog_production_entry_is_plan_generation(client: TestClient) -> None:
    _profile(client)
    adapter = _adapter(client, ToolId.EXERCISE_CATALOG_SEARCH)
    before = adapter.invocation_count
    response = client.post("/api/v1/plans/generate", json=_future_generation_payload())
    assert response.status_code == 201, response.text
    assert adapter.invocation_count - before == 1
    trace = _traces(client, ToolId.EXERCISE_CATALOG_SEARCH)[-1]
    assert (trace.caller, trace.tool_version) == (
        ToolCaller.PLAN_GENERATION_APPLICATION.value,
        "phase-8a-v1",
    )


def test_duration_production_entry_is_session_design(client: TestClient) -> None:
    _profile(client)
    adapter = _adapter(client, ToolId.SESSION_DURATION_CALCULATOR)
    before = adapter.invocation_count
    response = client.post(
        "/api/v1/session-designs", json=_session_payload("seven-tool-duration")
    )
    assert response.status_code == 201, response.text
    assert adapter.invocation_count - before == 1
    assert response.json()["duration_policy_version"] == "session-duration-policy-v1"
    trace = _traces(client, ToolId.SESSION_DURATION_CALCULATOR)[-1]
    assert trace.caller == ToolCaller.SESSION_DESIGN_APPLICATION.value


def test_recovery_spacing_production_entry_is_recovery_draft(
    client: TestClient,
) -> None:
    plan = _confirmed_plan(client)
    adapter = _adapter(client, ToolId.RECOVERY_SPACING_VALIDATOR)
    before = adapter.invocation_count
    response = client.post(
        "/api/v1/recovery-drafts",
        json=_recovery_payload(plan, "seven-tool-recovery"),
    )
    assert response.status_code == 201, response.text
    assert adapter.invocation_count - before == 1
    trace = _traces(client, ToolId.RECOVERY_SPACING_VALIDATOR)[-1]
    assert (trace.caller, trace.tool_version, trace.attempt_no) == (
        ToolCaller.RECOVERY_APPLICATION.value,
        "phase-8a-v1",
        1,
    )


def test_memory_candidate_production_entry_is_pending_review(
    client: TestClient,
) -> None:
    adapter = _adapter(client, ToolId.MEMORY_CANDIDATE_CREATE)
    before = adapter.invocation_count
    response = client.post(
        "/api/v1/memory-candidates",
        json=_candidate_payload("seven-tool-memory"),
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "PENDING_REVIEW"
    assert adapter.invocation_count - before == 1
    trace = _traces(client, ToolId.MEMORY_CANDIDATE_CREATE)[-1]
    assert (trace.caller, trace.tool_version) == (
        ToolCaller.MEMORY_COMMITTER.value,
        "phase-8a-v1",
    )
