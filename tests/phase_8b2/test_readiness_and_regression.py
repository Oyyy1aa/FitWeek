"""Readiness extension and Phase 8B1 contract protection."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.alerting.validation import AlertingAssetValidator
from app.config import get_settings
from app.main import create_application

pytestmark = pytest.mark.phase_8b2


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ALERTING_ENABLED", "true")
    monkeypatch.setenv("ALERT_NOTIFICATION_SINK", "in_memory")
    get_settings.cache_clear()
    with TestClient(create_application()) as value:
        yield value
    get_settings.cache_clear()


def test_readiness_adds_available_alerting_summary(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    alerting = response.json()["alerting"]
    assert alerting == {
        "status": "AVAILABLE",
        "dashboard_definitions_valid": True,
        "recording_rules_valid": True,
        "alert_rules_valid": True,
        "routing_policy_valid": True,
        "notification_sink": "in_memory",
    }


def test_alerting_disabled_keeps_business_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ALERTING_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_application()) as value:
        response = value.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["alerting"]["status"] == "DISABLED"
        assert value.get("/health/live").status_code == 200
    get_settings.cache_clear()


def test_notification_sink_failure_does_not_change_readiness(
    client: TestClient,
) -> None:
    runtime = client.app.state.alerting
    runtime.dispatcher.failures = 1
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_static_assets_validate_with_observability_disabled(project_root) -> None:
    result = AlertingAssetValidator(project_root).validate()
    assert result.valid


def test_phase_8b1_metric_names_remain_registered(client: TestClient) -> None:
    metrics = client.get("/metrics").text
    for name in (
        "http_server_requests_total",
        "fitweek_agent_runs_total",
        "fitweek_model_invocations_total",
        "fitweek_tool_invocations_total",
        "fitweek_memory_queries_total",
    ):
        assert name in metrics


def test_dashboard_and_alerting_are_not_business_dependencies(
    client: TestClient,
) -> None:
    runtime = client.app.state.alerting
    runtime.validation = runtime.validation.__class__(
        False, False, False, False, ("test",)
    )
    assert client.get("/health/live").status_code == 200
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["alerting"]["status"] == "DEGRADED"
