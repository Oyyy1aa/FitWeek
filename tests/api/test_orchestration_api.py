"""Real ASGI lifecycle contract for the Phase 2A control plane."""

import time
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_application
from tests.api.helpers import generation_payload, profile_payload

pytestmark = pytest.mark.phase_2a


@pytest.fixture
def orchestrator_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "true")
    monkeypatch.setenv("ORCHESTRATOR_WORKER_COUNT", "2")
    monkeypatch.setenv("ORCHESTRATOR_POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("ORCHESTRATOR_REAPER_INTERVAL_SECONDS", "0.05")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        yield client
    get_settings.cache_clear()


def run_payload(client_request_id: str) -> dict[str, object]:
    return {"client_request_id": client_request_id, **generation_payload()}


def wait_for_status(
    client: TestClient,
    run_id: str,
    target: str,
    *,
    timeout_seconds: float = 3,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/planning-runs/{run_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] == target:
            return body
        time.sleep(0.01)
    pytest.fail(f"run {run_id} did not reach {target}")


def test_http_workflow_query_confirm_finalize_and_metrics(
    orchestrator_client: TestClient,
) -> None:
    client = orchestrator_client
    assert client.put("/api/v1/profiles/me", json=profile_payload()).status_code == 200
    payload = run_payload("http-workflow-1")
    created = client.post("/api/v1/planning-runs", json=payload)
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    waiting = wait_for_status(client, run_id, "WAITING_CONFIRMATION")
    plan_id = waiting["result_reference"]

    steps = client.get(f"/api/v1/planning-runs/{run_id}/steps")
    checkpoints = client.get(f"/api/v1/planning-runs/{run_id}/checkpoints")
    audit = client.get(f"/api/v1/planning-runs/{run_id}/audit")
    assert steps.status_code == checkpoints.status_code == audit.status_code == 200
    assert len(steps.json()) == 4
    assert len(checkpoints.json()) == 3
    assert [item["sequence_no"] for item in audit.json()] == list(
        range(1, len(audit.json()) + 1)
    )
    plan = client.get(f"/api/v1/plans/{plan_id}").json()
    confirmed = client.post(
        f"/api/v1/planning-runs/{run_id}/confirm",
        json={"expected_plan_version": plan["version"]},
    )
    assert confirmed.status_code == 200
    completed = wait_for_status(client, run_id, "COMPLETED")
    assert completed["result_reference"] == plan_id
    assert client.get(f"/api/v1/plans/{plan_id}").json()["status"] == "CONFIRMED"
    final_steps = client.get(f"/api/v1/planning-runs/{run_id}/steps").json()
    final_checkpoints = client.get(f"/api/v1/planning-runs/{run_id}/checkpoints").json()
    assert len(final_steps) == len(final_checkpoints) == 5
    metrics = client.get("/api/v1/orchestrator/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["runs_completed"] == 1
    assert metrics.json()["steps_succeeded"] == 5

    duplicate = client.post("/api/v1/planning-runs", json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["run_id"] == run_id
    assert len(client.get("/api/v1/plans").json()) == 1


def test_http_negative_control_plane_cases(orchestrator_client: TestClient) -> None:
    client = orchestrator_client
    client.put("/api/v1/profiles/me", json=profile_payload())
    payload = run_payload("http-negative-1")
    run_id = client.post("/api/v1/planning-runs", json=payload).json()["run_id"]
    conflict_payload = run_payload("http-negative-1")
    conflict_payload["preferred_locations"] = ["GYM"]
    conflict = client.post("/api/v1/planning-runs", json=conflict_payload)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "RUN_IDEMPOTENCY_CONFLICT"

    cancelled = client.post(f"/api/v1/planning-runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    repeated_cancel = client.post(f"/api/v1/planning-runs/{run_id}/cancel")
    assert repeated_cancel.status_code == 200
    early_confirm = client.post(
        f"/api/v1/planning-runs/{run_id}/confirm",
        json={"expected_plan_version": 1},
    )
    assert early_confirm.status_code == 409
    assert "traceback" not in early_confirm.text.casefold()

    missing = client.get(f"/api/v1/planning-runs/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_memory_readiness_reports_worker_pool(orchestrator_client: TestClient) -> None:
    response = orchestrator_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {
        "persistence": "memory",
        "redis": "disabled",
        "orchestrator": "ok",
    }


def test_disabled_orchestrator_rejects_run_creation(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/v1/planning-runs",
        json=run_payload("disabled-orchestrator"),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ORCHESTRATOR_DISABLED"
