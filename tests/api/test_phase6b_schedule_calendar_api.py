"""Phase 6B API contracts across Schedule Apply, ICS, and Calendar writes."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import BusinessContainer
from app.config import get_settings
from app.domain.tools.enums import ToolId
from app.main import create_application
from tests.api.helpers import generation_payload, profile_payload

pytestmark = pytest.mark.phase_6b


@pytest.fixture
def calendar_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("CALENDAR_WRITE_ENABLED", "true")
    monkeypatch.setenv("CALENDAR_WRITE_PROVIDER", "scripted")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "true")
    monkeypatch.setenv("ORCHESTRATOR_POLL_INTERVAL_SECONDS", "0.01")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        yield client
    get_settings.cache_clear()


def _confirmed_plan(client: TestClient) -> dict[str, object]:
    assert client.put("/api/v1/profiles/me", json=profile_payload()).status_code == 200
    generated = client.post("/api/v1/plans/generate", json=generation_payload())
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed = client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _schedule_payload(plan: dict[str, object]) -> dict[str, object]:
    windows = []
    for day in (20, 22):
        start = datetime(2026, 7, day, 12, tzinfo=UTC)
        windows.append(
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=2)).isoformat(),
                "location": "HOME",
            }
        )
    return {
        "client_request_id": "phase6b-schedule",
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "timezone": "UTC",
        "availability_windows": windows,
        "manual_busy_windows": [],
    }


def _apply_schedule(client: TestClient, plan: dict[str, object]) -> dict[str, object]:
    draft = client.post("/api/v1/schedule-drafts", json=_schedule_payload(plan)).json()
    accepted_response = client.post(
        f"/api/v1/schedule-drafts/{draft['id']}/accept",
        json={"expected_version": draft["version"]},
    )
    assert accepted_response.status_code == 200
    accepted = accepted_response.json()
    payload = {
        "client_request_id": "phase6b-apply",
        "expected_draft_version": accepted["version"],
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
    }
    preview = client.post(
        f"/api/v1/schedule-drafts/{draft['id']}/apply-preview", json=payload
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["passed"] is True
    applied = client.post(f"/api/v1/schedule-drafts/{draft['id']}/apply", json=payload)
    assert applied.status_code == 201, applied.text
    body = applied.json()
    assert body["plan"]["status"] == "VALIDATED"
    assert body["plan"]["revision"] == 2
    second = client.post(f"/api/v1/schedule-drafts/{draft['id']}/apply", json=payload)
    assert second.status_code == 200
    assert second.json()["result"]["id"] == body["result"]["id"]
    return body


def _wait_run(
    client: TestClient, path: str, expected: set[str], timeout: float = 4
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(path)
        assert response.status_code == 200, response.text
        latest = response.json()
        if latest["status"] in expected:
            return latest
        time.sleep(0.01)
    raise AssertionError(f"Run did not reach {expected}: {latest}")


def test_schedule_apply_is_revision_only_and_requires_confirmation(
    calendar_client: TestClient,
) -> None:
    source = _confirmed_plan(calendar_client)
    applied = _apply_schedule(calendar_client, source)
    root = source["root_plan_id"] or source["id"]
    revisions = calendar_client.get(f"/api/v1/plans/{root}/revisions").json()
    assert [item["plan"]["revision"] for item in revisions] == [1, 2]
    assert revisions[0]["is_current_revision"] is True
    assert revisions[1]["is_current_revision"] is False
    original_sessions = source["sessions"]
    revised_sessions = applied["plan"]["sessions"]
    for old, new in zip(original_sessions, revised_sessions, strict=True):
        assert old["exercises"] == new["exercises"]
        assert old["location_type"] == new["location_type"]
        assert old["session_type"] == new["session_type"]
        assert old["estimated_minutes"] == new["estimated_minutes"]
    confirmed = calendar_client.post(
        f"/api/v1/plans/{root}/revisions/2/confirm",
        json={"expected_version": applied["plan"]["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["is_current_revision"] is True


def test_ics_is_deterministic_and_does_not_create_bindings(
    calendar_client: TestClient,
) -> None:
    plan = _confirmed_plan(calendar_client)
    root = plan["root_plan_id"] or plan["id"]
    payload = {
        "client_request_id": "ics-1",
        "expected_plan_version": plan["version"],
    }
    first = calendar_client.post(
        f"/api/v1/plans/{root}/revisions/1/ics-export", json=payload
    )
    second = calendar_client.post(
        f"/api/v1/plans/{root}/revisions/1/ics-export", json=payload
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["content_sha256"] == second.json()["content_sha256"]
    export_id = first.json()["id"]
    download = calendar_client.get(f"/api/v1/ics-exports/{export_id}/download")
    assert download.status_code == 200
    assert download.content.startswith(b"BEGIN:VCALENDAR\r\n")
    assert download.content.endswith(b"END:VCALENDAR\r\n")
    bindings = calendar_client.get(
        "/api/v1/calendar-bindings",
        params={
            "provider": "scripted",
            "calendar_id": "primary",
            "root_plan_id": root,
        },
    )
    assert bindings.json() == []


def test_calendar_draft_is_side_effect_free_until_explicit_execution(
    calendar_client: TestClient,
) -> None:
    plan = _confirmed_plan(calendar_client)
    root = plan["root_plan_id"] or plan["id"]
    payload = {
        "client_request_id": "calendar-1",
        "expected_plan_version": plan["version"],
        "provider": "scripted",
        "calendar_id": "primary",
    }
    created = calendar_client.post(
        f"/api/v1/plans/{root}/revisions/1/calendar-operation-drafts",
        json=payload,
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert {item["operation_type"] for item in draft["items"]} == {"CREATE"}
    assert (
        calendar_client.get(
            "/api/v1/calendar-bindings",
            params={
                "provider": "scripted",
                "calendar_id": "primary",
                "root_plan_id": root,
            },
        ).json()
        == []
    )
    approved = calendar_client.post(
        f"/api/v1/calendar-operation-drafts/{draft['id']}/approve",
        json={"expected_version": draft["version"]},
    )
    assert approved.status_code == 200
    container = calendar_client.app.state.business_container
    assert isinstance(container, BusinessContainer)
    adapter = container.tool_gateway.registry.get(
        ToolId.CALENDAR_COMMIT.value, "phase-8a-v1"
    ).adapter
    provider_calls_before = adapter.invocation_count
    draft_before = calendar_client.get(
        f"/api/v1/calendar-operation-drafts/{draft['id']}"
    ).json()
    attempts_before = calendar_client.get(
        f"/api/v1/calendar-operation-drafts/{draft['id']}/attempts"
    ).json()
    bindings_before = calendar_client.get(
        "/api/v1/calendar-bindings",
        params={"provider": "scripted", "calendar_id": "primary", "root_plan_id": root},
    ).json()
    executed = calendar_client.post(
        f"/api/v1/calendar-operation-drafts/{draft['id']}/execute"
    )
    assert executed.status_code == 409, executed.text
    assert (
        calendar_client.post(
            f"/api/v1/calendar-operation-drafts/{draft['id']}/retry"
        ).status_code
        == 409
    )
    assert adapter.invocation_count == provider_calls_before
    assert (
        calendar_client.get(f"/api/v1/calendar-operation-drafts/{draft['id']}").json()
        == draft_before
    )
    assert (
        calendar_client.get(
            f"/api/v1/calendar-operation-drafts/{draft['id']}/attempts"
        ).json()
        == attempts_before
    )
    assert (
        calendar_client.get(
            "/api/v1/calendar-bindings",
            params={
                "provider": "scripted",
                "calendar_id": "primary",
                "root_plan_id": root,
            },
        ).json()
        == bindings_before
    )
    run = calendar_client.post(
        "/api/v1/calendar-operation-runs",
        json={"client_request_id": "calendar-1-run", "draft_id": draft["id"]},
    )
    assert run.status_code == 202, run.text
    completed = _wait_run(
        calendar_client,
        f"/api/v1/calendar-operation-runs/{run.json()['id']}",
        {"COMPLETED", "FAILED_PERMANENT"},
    )
    assert completed["status"] == "COMPLETED", completed
    bindings = calendar_client.get(
        "/api/v1/calendar-bindings",
        params={
            "provider": "scripted",
            "calendar_id": "primary",
            "root_plan_id": root,
        },
    ).json()
    assert len(bindings) == len(plan["sessions"])
    assert all("external_event_id" not in item for item in bindings)
    attempts = calendar_client.get(
        f"/api/v1/calendar-operation-drafts/{draft['id']}/attempts"
    )
    assert attempts.status_code == 200
    assert len(attempts.json()) == len(plan["sessions"])


def test_rejected_calendar_draft_never_executes(calendar_client: TestClient) -> None:
    plan = _confirmed_plan(calendar_client)
    root = plan["root_plan_id"] or plan["id"]
    created = calendar_client.post(
        f"/api/v1/plans/{root}/revisions/1/calendar-operation-drafts",
        json={
            "client_request_id": "calendar-reject",
            "expected_plan_version": plan["version"],
            "provider": "scripted",
            "calendar_id": "primary",
        },
    ).json()
    rejected = calendar_client.post(
        f"/api/v1/calendar-operation-drafts/{created['id']}/reject",
        json={"expected_version": created["version"]},
    )
    assert rejected.status_code == 200
    execution = calendar_client.post(
        f"/api/v1/calendar-operation-drafts/{created['id']}/execute"
    )
    assert execution.status_code == 409
    assert "traceback" not in execution.text.lower()


def test_schedule_and_calendar_orchestrator_workflows_complete(
    calendar_client: TestClient,
) -> None:
    source = _confirmed_plan(calendar_client)
    draft = calendar_client.post(
        "/api/v1/schedule-drafts", json=_schedule_payload(source)
    ).json()
    accepted = calendar_client.post(
        f"/api/v1/schedule-drafts/{draft['id']}/accept",
        json={"expected_version": draft["version"]},
    ).json()
    root = source["root_plan_id"] or source["id"]
    run_response = calendar_client.post(
        "/api/v1/schedule-application-runs",
        json={
            "client_request_id": "schedule-run-1",
            "draft_id": accepted["id"],
            "expected_draft_version": accepted["version"],
            "root_plan_id": root,
            "source_revision": source["revision"],
            "expected_plan_version": source["version"],
        },
    )
    assert run_response.status_code == 202, run_response.text
    run_id = run_response.json()["id"]
    waiting = _wait_run(
        calendar_client,
        f"/api/v1/schedule-application-runs/{run_id}",
        {"WAITING_CONFIRMATION", "FAILED_PERMANENT"},
    )
    assert waiting["status"] == "WAITING_CONFIRMATION", waiting
    revision = calendar_client.get(f"/api/v1/plans/{root}/revisions/2").json()
    confirm = calendar_client.post(
        f"/api/v1/schedule-application-runs/{run_id}/confirm",
        json={
            "expected_revision": 2,
            "expected_plan_version": revision["plan"]["version"],
        },
    )
    assert confirm.status_code == 202, confirm.text
    completed = _wait_run(
        calendar_client,
        f"/api/v1/schedule-application-runs/{run_id}",
        {"COMPLETED", "FAILED_PERMANENT"},
    )
    assert completed["status"] == "COMPLETED", completed

    current = calendar_client.get(f"/api/v1/plans/{root}/revisions/2").json()["plan"]
    operation = calendar_client.post(
        f"/api/v1/plans/{root}/revisions/2/calendar-operation-drafts",
        json={
            "client_request_id": "calendar-run-draft",
            "expected_plan_version": current["version"],
            "provider": "scripted",
            "calendar_id": "primary",
        },
    ).json()
    approved = calendar_client.post(
        f"/api/v1/calendar-operation-drafts/{operation['id']}/approve",
        json={"expected_version": operation["version"]},
    )
    assert approved.status_code == 200
    calendar_run = calendar_client.post(
        "/api/v1/calendar-operation-runs",
        json={
            "client_request_id": "calendar-run-1",
            "draft_id": operation["id"],
        },
    )
    assert calendar_run.status_code == 202, calendar_run.text
    calendar_run_id = calendar_run.json()["id"]
    calendar_completed = _wait_run(
        calendar_client,
        f"/api/v1/calendar-operation-runs/{calendar_run_id}",
        {"COMPLETED", "FAILED_PERMANENT"},
    )
    assert calendar_completed["status"] == "COMPLETED", calendar_completed
    checkpoints = calendar_client.get(
        f"/api/v1/calendar-operation-runs/{calendar_run_id}/checkpoints"
    )
    assert checkpoints.status_code == 200
    assert len(checkpoints.json()) == 5
