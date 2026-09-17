"""Circuit, timeout, and NO_MEMORY degradation export evidence."""

import time
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import BusinessContainer
from app.config import get_settings
from app.domain.memory.errors import MemoryQueryFailedError
from app.main import create_application
from tests.phase_8a.test_real_http_fault_closure import (
    _assert_clean_logs,
    _confirmed_plan,
    _running_stack,
    _schedule_payload,
)

pytestmark = pytest.mark.phase_8b1


@pytest.fixture
def memory_failure_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("MODEL_PRIMARY_PROVIDER", "scripted-fake")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        yield client


def test_real_calendar_circuit_degradation_is_exported(tmp_path) -> None:
    with _running_stack(tmp_path, failure_threshold=1) as (api, stub, logs):
        plan = _confirmed_plan(api)
        assert (
            stub.post(
                "/admin/read-control", json={"scenario": "server-error"}
            ).status_code
            == 200
        )
        first = api.post(
            "/api/v1/schedule-drafts",
            json=_schedule_payload(plan, "phase-8b1-circuit-open"),
        )
        assert first.status_code == 201
        assert first.json()["calendar_verification_status"] == "MANUAL_ONLY"
        count = stub.get("/admin/read-stats").json()["request_count"]
        second = api.post(
            "/api/v1/schedule-drafts",
            json=_schedule_payload(plan, "phase-8b1-circuit-rejected"),
        )
        assert second.status_code == 201
        assert stub.get("/admin/read-stats").json()["request_count"] == count
        metrics = api.get("/metrics").text
        assert "fitweek_tool_circuit_open_total" in metrics
        assert "fitweek_tool_circuit_rejections_total" in metrics
        assert 'degradation_mode="MANUAL_ONLY"' in metrics
    _assert_clean_logs(logs)


def test_real_calendar_half_open_recovery_is_exported(tmp_path) -> None:
    with _running_stack(tmp_path, failure_threshold=1) as (api, stub, logs):
        plan = _confirmed_plan(api)
        stub.post("/admin/read-control", json={"scenario": "server-error"})
        api.post(
            "/api/v1/schedule-drafts",
            json=_schedule_payload(plan, "phase-8b1-open-before-probe"),
        )
        stub.post("/admin/read-control", json={"scenario": "success"})
        time.sleep(1.05)
        recovered = api.post(
            "/api/v1/schedule-drafts",
            json=_schedule_payload(plan, "phase-8b1-half-open-success"),
        )
        assert recovered.status_code == 201
        assert recovered.json()["calendar_verification_status"] == "VERIFIED"
        summaries = api.get("/api/v1/tool-gateway/invocations").json()
        assert summaries[-1]["circuit_before"] == "HALF_OPEN"
        assert summaries[-1]["circuit_after"] == "CLOSED"
    _assert_clean_logs(logs)


def test_real_tool_deadline_is_exported(tmp_path) -> None:
    with _running_stack(tmp_path) as (api, stub, logs):
        plan = _confirmed_plan(api)
        stub.post(
            "/admin/read-control",
            json={"scenario": "success", "delay_seconds": 0.4},
        )
        response = api.post(
            "/api/v1/schedule-drafts",
            json=_schedule_payload(plan, "phase-8b1-deadline"),
        )
        assert response.status_code == 201
        assert response.json()["calendar_verification_status"] == "MANUAL_ONLY"
        metrics = api.get("/metrics").text
        assert "fitweek_tool_deadline_exceeded_total" in metrics
        assert 'error_category="DEADLINE_EXCEEDED"' in metrics
    _assert_clean_logs(logs)


def test_profile_agent_continues_with_no_memory_and_exports_degradation(
    memory_failure_client: TestClient,
) -> None:
    container = memory_failure_client.app.state.business_container
    assert isinstance(container, BusinessContainer)
    failing = AsyncMock(
        side_effect=MemoryQueryFailedError("private repository failure")
    )
    container.memory_repository.list_active_for_context = failing  # type: ignore[method-assign]
    before = len(container.observability.tracing.finished_spans())
    response = memory_failure_client.post(
        "/api/v1/profile-agent/parse",
        json={
            "client_request_id": "phase-8b1-no-memory",
            "user_message": "Two short home workouts.",
            "current_week": "2026-07-20",
        },
    )
    assert response.status_code == 201, response.text
    assert failing.await_count == 2
    metrics = memory_failure_client.get("/metrics").text
    assert "fitweek_memory_no_memory_total" in metrics
    assert 'outcome="DEGRADED"' in metrics
    spans = container.observability.tracing.finished_spans()[before:]
    memory_span = next(span for span in spans if span.name == "memory.query")
    assert memory_span.attributes["degradation_mode"] == "NO_MEMORY"
    assert "private repository failure" not in str(memory_span.attributes)
