"""Formal Agent, Model, Context, and Orchestrator span integration."""

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import BusinessContainer
from app.config import get_settings
from app.main import create_application
from tests.api.helpers import profile_payload
from tests.phase_8b1.test_http_observability import _generation_payload

pytestmark = pytest.mark.phase_8b1


@pytest.fixture
def agent_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("MODEL_PRIMARY_PROVIDER", "scripted-fake")
    monkeypatch.setenv("MODEL_BACKUP_PROVIDER", "template-fallback")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        yield client


@pytest.fixture
def orchestrator_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "true")
    monkeypatch.setenv("ORCHESTRATOR_WORKER_COUNT", "1")
    monkeypatch.setenv("ORCHESTRATOR_POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("ORCHESTRATOR_REAPER_INTERVAL_SECONDS", "0.05")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        yield client


def _container(client: TestClient) -> BusinessContainer:
    container = client.app.state.business_container
    assert isinstance(container, BusinessContainer)
    return container


def test_profile_agent_span_contains_context_and_model_children(
    agent_client: TestClient,
) -> None:
    facade = _container(agent_client).observability
    before = len(facade.tracing.finished_spans())
    response = agent_client.post(
        "/api/v1/profile-agent/parse",
        json={
            "client_request_id": "phase-8b1-profile-agent",
            "user_message": "I prefer three home workouts each week.",
            "current_week": "2026-07-20",
        },
    )
    assert response.status_code == 201, response.text
    spans = facade.tracing.finished_spans()[before:]
    by_name = {span.name: span for span in spans}
    assert {
        "agent.invocation",
        "context.build_snapshot",
        "model.gateway.invocation",
        "model.attempt",
    } <= set(by_name)
    agent = by_name["agent.invocation"]
    assert by_name["context.build_snapshot"].context.trace_id == agent.context.trace_id
    assert (
        by_name["model.gateway.invocation"].context.trace_id == agent.context.trace_id
    )
    assert "user_message" not in str(agent.attributes)


def test_model_and_agent_metrics_are_exported(agent_client: TestClient) -> None:
    agent_client.post(
        "/api/v1/profile-agent/parse",
        json={
            "client_request_id": "phase-8b1-profile-metrics",
            "user_message": "Home training twice weekly.",
            "current_week": "2026-07-20",
        },
    )
    text = agent_client.get("/metrics").text
    assert "fitweek_agent_runs_total" in text
    assert "fitweek_model_invocations_total" in text
    assert "fitweek_model_attempts_total" in text


def test_orchestrator_worker_exports_run_and_step_spans(
    orchestrator_client: TestClient,
) -> None:
    assert (
        orchestrator_client.put(
            "/api/v1/profiles/me", json=profile_payload()
        ).status_code
        == 200
    )
    created = orchestrator_client.post(
        "/api/v1/planning-runs",
        json={
            "client_request_id": "phase-8b1-orchestrator",
            **_generation_payload(),
        },
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    deadline = time.monotonic() + 4
    status = None
    while time.monotonic() < deadline:
        status = orchestrator_client.get(f"/api/v1/planning-runs/{run_id}").json()[
            "status"
        ]
        if status == "WAITING_CONFIRMATION":
            break
        time.sleep(0.01)
    assert status == "WAITING_CONFIRMATION"
    spans = _container(orchestrator_client).observability.tracing.finished_spans()
    run_spans = [span for span in spans if span.name == "orchestrator.run"]
    step_spans = [span for span in spans if span.name == "orchestrator.step"]
    assert run_spans and step_spans
    assert all(span.attributes["run_id"] == run_id for span in run_spans)
    assert all("step_type" in span.attributes for span in step_spans)


def test_orchestrator_audit_remains_separate_from_telemetry(
    orchestrator_client: TestClient,
) -> None:
    assert (
        orchestrator_client.put(
            "/api/v1/profiles/me", json=profile_payload()
        ).status_code
        == 200
    )
    created = orchestrator_client.post(
        "/api/v1/planning-runs",
        json={
            "client_request_id": "phase-8b1-audit-separation",
            **_generation_payload(),
        },
    )
    run_id = created.json()["run_id"]
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        body = orchestrator_client.get(f"/api/v1/planning-runs/{run_id}").json()
        if body["status"] == "WAITING_CONFIRMATION":
            break
        time.sleep(0.01)
    audit = orchestrator_client.get(f"/api/v1/planning-runs/{run_id}/audit")
    assert audit.status_code == 200
    assert audit.json()
    assert all("event_type" in item for item in audit.json())
