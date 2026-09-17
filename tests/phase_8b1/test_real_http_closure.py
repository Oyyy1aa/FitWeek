"""Real Uvicorn HTTP evidence on a dynamic localhost port."""

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app.api.dependencies import BusinessContainer
from app.config import get_settings
from app.main import create_application
from app.observability.logging import InMemoryStructuredLogSink
from tests.api.helpers import profile_payload

pytestmark = pytest.mark.phase_8b1


@dataclass(slots=True)
class RunningApplication:
    app: FastAPI
    client: httpx.Client


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def real_server() -> Iterator[RunningApplication]:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER", "in_memory")
    monkeypatch.setenv("PROMETHEUS_METRICS_ENABLED", "true")
    monkeypatch.setenv("STRUCTURED_LOGGING_ENABLED", "true")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("MODEL_PRIMARY_PROVIDER", "scripted-fake")
    monkeypatch.setenv("MODEL_BACKUP_PROVIDER", "template-fallback")
    monkeypatch.setenv("SCHEDULE_AGENT_ENABLED", "true")
    monkeypatch.setenv("RECOVERY_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    application = create_application()
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host="127.0.0.1",
            port=port,
            log_config=None,
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("Uvicorn did not start within ten seconds.")
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
        yield RunningApplication(application, client)
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()
    monkeypatch.undo()
    get_settings.cache_clear()


def _container(server: RunningApplication) -> BusinessContainer:
    container = server.app.state.business_container
    assert isinstance(container, BusinessContainer)
    return container


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
            for offset in (1, 3)
        ],
        "preferred_locations": ["HOME"],
        "preferred_session_types": [],
    }


def test_real_http_root_span(real_server: RunningApplication) -> None:
    facade = _container(real_server).observability
    before = len(facade.tracing.finished_spans())
    response = real_server.client.get("/health/live")
    assert response.status_code == 200
    spans = facade.tracing.finished_spans()[before:]
    assert any(span.name == "http.request" for span in spans)
    assert response.headers["x-correlation-id"]


def test_real_http_prometheus_endpoint(real_server: RunningApplication) -> None:
    real_server.client.get("/health/live")
    response = real_server.client.get("/metrics")
    assert response.status_code == 200
    assert "http_server_requests_total" in response.text
    assert "http_server_request_duration_seconds_bucket" in response.text


def test_real_http_readiness_summary(real_server: RunningApplication) -> None:
    response = real_server.client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["observability"]["exporter_status"] == "AVAILABLE"


def test_real_http_business_rejection_span(real_server: RunningApplication) -> None:
    facade = _container(real_server).observability
    before = len(facade.tracing.finished_spans())
    response = real_server.client.get("/api/v1/plans/not-a-uuid")
    assert response.status_code == 422
    root = [
        span
        for span in facade.tracing.finished_spans()[before:]
        if span.name == "http.request"
    ][-1]
    assert root.attributes["outcome"] == "REJECTED"


def test_real_http_route_label_has_no_uuid_value(
    real_server: RunningApplication,
) -> None:
    real_server.client.get("/api/v1/plans/00000000-0000-4000-8000-000000000099")
    metrics = real_server.client.get("/metrics").text
    assert 'route="/api/v1/plans/{plan_id}"' in metrics
    assert "00000000-0000-4000-8000-000000000099" not in metrics


def test_real_http_json_log_context(real_server: RunningApplication) -> None:
    facade = _container(real_server).observability
    sink = InMemoryStructuredLogSink()
    facade.log_sink = sink
    response = real_server.client.get("/health/live")
    item = sink.items()[-1]
    assert item["correlation_id"] == response.headers["x-correlation-id"]
    assert item["trace_id"] and item["span_id"]
    assert "traceback" not in str(item).casefold()


def test_real_http_plan_tool_and_safety_hierarchy(
    real_server: RunningApplication,
) -> None:
    assert (
        real_server.client.put(
            "/api/v1/profiles/me", json=profile_payload()
        ).status_code
        == 200
    )
    facade = _container(real_server).observability
    before = len(facade.tracing.finished_spans())
    response = real_server.client.post(
        "/api/v1/plans/generate", json=_future_generation_payload()
    )
    assert response.status_code == 201, response.text
    plan: dict[str, Any] = response.json()["plan"]
    assert plan["sessions"]
    spans = facade.tracing.finished_spans()[before:]
    names = {span.name for span in spans}
    assert {
        "http.request",
        "tool.invocation",
        "tool.attempt",
        "safety.validation",
    } <= names
    traces = {span.context.trace_id for span in spans if span.name in names}
    assert len(traces) == 1


def test_real_http_cross_feature_observability_closure(
    real_server: RunningApplication,
) -> None:
    client = real_server.client
    profile_draft = client.post(
        "/api/v1/profile-agent/parse",
        json={
            "client_request_id": "phase-8b1-real-profile",
            "user_message": "Two home training sessions per week.",
            "current_week": "2026-07-20",
        },
    )
    assert profile_draft.status_code == 201, profile_draft.text

    profile = client.put("/api/v1/profiles/me", json=profile_payload())
    assert profile.status_code == 200
    generated = client.post("/api/v1/plans/generate", json=_future_generation_payload())
    assert generated.status_code in {200, 201}, generated.text
    plan = generated.json()["plan"]
    confirmed = client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    plan = confirmed.json()

    session_design = client.post(
        "/api/v1/session-designs",
        json={
            "client_request_id": "phase-8b1-real-session",
            "target_date": plan["sessions"][0]["scheduled_start"][:10],
            "target_duration_minutes": 30,
            "location": "HOME",
            "goal": "GENERAL_FITNESS",
        },
    )
    assert session_design.status_code == 201, session_design.text

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
    schedule = client.post(
        "/api/v1/schedule-drafts",
        json={
            "client_request_id": "phase-8b1-real-schedule",
            "root_plan_id": plan["root_plan_id"] or plan["id"],
            "source_revision": plan["revision"],
            "expected_plan_version": plan["version"],
            "timezone": "UTC",
            "availability_windows": windows,
            "manual_busy_windows": [],
        },
    )
    assert schedule.status_code == 201, schedule.text

    exported = client.post(
        f"/api/v1/plans/{plan['root_plan_id'] or plan['id']}"
        f"/revisions/{plan['revision']}/ics-export",
        json={
            "client_request_id": "phase-8b1-real-ics",
            "expected_plan_version": plan["version"],
        },
    )
    assert exported.status_code == 201, exported.text

    recovery = client.post(
        "/api/v1/recovery-drafts",
        json={
            "client_request_id": "phase-8b1-real-recovery",
            "root_plan_id": plan["root_plan_id"] or plan["id"],
            "source_revision": plan["revision"],
            "expected_plan_version": plan["version"],
            "request_type": "RESCHEDULE_REQUEST",
            "target_session_ids": [plan["sessions"][-1]["id"]],
            "user_request": "Move one future workout.",
        },
    )
    assert recovery.status_code == 201, recovery.text

    candidate = client.post(
        "/api/v1/memory-candidates",
        json={
            "client_request_id": "phase-8b1-real-memory",
            "memory_type": "PREFERRED_LOCATION",
            "key": "preferred_location",
            "value": "home",
            "source": "PROFILE_AGENT_CANDIDATE",
            "source_reference": "draft:phase-8b1-real-profile",
            "evidence_summary": "Explicit structured candidate evidence.",
            "confidence": "0.8",
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert candidate.status_code == 201, candidate.text

    metrics = client.get("/metrics").text
    for metric in (
        "fitweek_agent_runs_total",
        "fitweek_model_invocations_total",
        "fitweek_tool_invocations_total",
        "fitweek_plan_generations_total",
        "fitweek_schedule_drafts_total",
        "fitweek_ics_exports_total",
        "fitweek_recovery_drafts_total",
        "fitweek_memory_candidates_created_total",
    ):
        assert metric in metrics
