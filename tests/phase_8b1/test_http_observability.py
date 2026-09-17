"""HTTP middleware, readiness, and formal application telemetry."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import BusinessContainer
from app.config import get_settings
from app.main import create_application
from app.observability.logging import InMemoryStructuredLogSink
from tests.api.helpers import profile_payload

pytestmark = pytest.mark.phase_8b1


def _container(client: TestClient) -> BusinessContainer:
    container = client.app.state.business_container
    assert isinstance(container, BusinessContainer)
    return container


def _generation_payload() -> dict[str, object]:
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


def _create_plan(client: TestClient) -> dict[str, Any]:
    assert client.put("/api/v1/profiles/me", json=profile_payload()).status_code == 200
    response = client.post("/api/v1/plans/generate", json=_generation_payload())
    assert response.status_code == 201, response.text
    return response.json()["plan"]


def test_metrics_endpoint_uses_prometheus_text(
    observability_client: TestClient,
) -> None:
    observability_client.get("/health/live")
    response = observability_client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "http_server_requests_total" in response.text


def test_readiness_adds_observability_without_removing_existing_fields(
    observability_client: TestClient,
) -> None:
    body = observability_client.get("/health/ready").json()
    assert body["status"] == "ready"
    assert body["checks"] == {"persistence": "memory", "redis": "disabled"}
    assert body["tool_gateway"]["registered_tool_count"] == 7
    assert body["observability"]["status"] == "AVAILABLE"


def test_http_request_creates_root_and_application_spans(
    observability_client: TestClient,
) -> None:
    facade = _container(observability_client).observability
    before = len(facade.tracing.finished_spans())
    response = observability_client.get("/health/live")
    assert response.status_code == 200
    spans = facade.tracing.finished_spans()[before:]
    names = {span.name for span in spans}
    assert {"http.request", "application.operation"} <= names


def test_http_span_uses_route_template_not_uuid(
    observability_client: TestClient,
) -> None:
    plan = _create_plan(observability_client)
    facade = _container(observability_client).observability
    before = len(facade.tracing.finished_spans())
    response = observability_client.get(f"/api/v1/plans/{plan['id']}")
    assert response.status_code == 200
    roots = [
        span
        for span in facade.tracing.finished_spans()[before:]
        if span.name == "http.request"
    ]
    assert roots
    route = roots[-1].attributes["http_route_template"]
    assert "{plan_id}" in route
    assert plan["id"] not in route


def test_business_rejection_is_observable_without_internal_exception(
    observability_client: TestClient,
) -> None:
    facade = _container(observability_client).observability
    before = len(facade.tracing.finished_spans())
    response = observability_client.get("/api/v1/plans/not-a-uuid")
    assert response.status_code == 422
    root = [
        span
        for span in facade.tracing.finished_spans()[before:]
        if span.name == "http.request"
    ][-1]
    assert root.attributes["outcome"] == "REJECTED"
    assert not root.events


def test_plan_generation_exports_tool_and_safety_children(
    observability_client: TestClient,
) -> None:
    facade = _container(observability_client).observability
    before = len(facade.tracing.finished_spans())
    _create_plan(observability_client)
    spans = facade.tracing.finished_spans()[before:]
    names = {span.name for span in spans}
    assert "tool.invocation" in names
    assert "tool.attempt" in names
    assert "safety.validation" in names
    root = next(
        span
        for span in spans
        if span.name == "http.request" and span.attributes["http_method"] == "POST"
    )
    tool = next(span for span in spans if span.name == "tool.invocation")
    assert root.context.trace_id == tool.context.trace_id


def test_plan_generation_exports_business_metric(
    observability_client: TestClient,
) -> None:
    _create_plan(observability_client)
    metrics = observability_client.get("/metrics").text
    assert 'fitweek_plan_generations_total{outcome="SUCCEEDED"} 1.0' in metrics
    assert "fitweek_tool_invocations_total" in metrics


def test_structured_log_correlates_with_http_span(
    observability_client: TestClient,
) -> None:
    facade = _container(observability_client).observability
    sink = InMemoryStructuredLogSink()
    facade.log_sink = sink
    response = observability_client.get("/health/live")
    correlation = response.headers["x-correlation-id"]
    item = sink.items()[-1]
    assert item["correlation_id"] == correlation
    assert item["trace_id"]
    assert item["span_id"]


def test_disabled_observability_keeps_business_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["observability"]["status"] == "DISABLED"
        assert client.get("/metrics").status_code == 404


def test_observability_degradation_does_not_change_readiness_status(
    observability_client: TestClient,
) -> None:
    facade = _container(observability_client).observability
    facade.mark_degraded("test-exporter")
    response = observability_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["observability"]["status"] == "DEGRADED"
