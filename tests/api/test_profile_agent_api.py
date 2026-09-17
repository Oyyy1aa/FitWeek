"""Draft-only Profile Agent HTTP behavior and side-effect boundaries."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_application

pytestmark = pytest.mark.phase_3a


@pytest.fixture
def profile_agent_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("MODEL_PRIMARY_PROVIDER", "scripted-fake")
    monkeypatch.setenv("MODEL_BACKUP_PROVIDER", "template-fallback")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        yield client
    get_settings.cache_clear()


def parse_payload(client_request_id: str = "profile-parse-1") -> dict[str, object]:
    return {
        "client_request_id": client_request_id,
        "user_message": "每周三次，每次30分钟，居家训练，只有弹力带。",
        "current_week": "2026-07-13",
    }


def test_parse_draft_trace_metrics_and_no_business_side_effects(
    profile_agent_client: TestClient,
) -> None:
    client = profile_agent_client
    before = client.get("/api/v1/model-gateway/metrics").json()
    created = client.post("/api/v1/profile-agent/parse", json=parse_payload())
    assert created.status_code == 201
    body = created.json()
    assert body["prompt_version"] == "profile-agent-v2"
    assert body["output"]["scope_status"] == "NEEDS_REVIEW"

    draft = client.get(f"/api/v1/profile-agent/drafts/{body['id']}")
    traces = client.get(f"/api/v1/profile-agent/requests/{body['request_id']}/traces")
    metrics = client.get("/api/v1/model-gateway/metrics")
    assert draft.status_code == traces.status_code == metrics.status_code == 200
    assert len(traces.json()) == 1
    assert traces.json()[0]["input_tokens"] is None
    assert "user_message" not in traces.text
    assert metrics.json()["provider_attempts_total"] == (
        before["provider_attempts_total"] + 1
    )
    assert client.get("/api/v1/profiles/me").status_code == 404
    assert client.get("/api/v1/plans").json() == []


def test_parse_idempotency_reuses_draft_without_provider_call(
    profile_agent_client: TestClient,
) -> None:
    client = profile_agent_client
    payload = parse_payload("same-request")
    first = client.post("/api/v1/profile-agent/parse", json=payload)
    attempts = client.get("/api/v1/model-gateway/metrics").json()[
        "provider_attempts_total"
    ]
    second = client.post("/api/v1/profile-agent/parse", json=payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert (
        client.get("/api/v1/model-gateway/metrics").json()["provider_attempts_total"]
        == attempts
    )


def test_idempotency_conflict_and_request_errors_use_safe_envelope(
    profile_agent_client: TestClient,
) -> None:
    client = profile_agent_client
    payload = parse_payload("conflict-request")
    assert client.post("/api/v1/profile-agent/parse", json=payload).status_code == 201
    payload["user_message"] = "different message"
    conflict = client.post("/api/v1/profile-agent/parse", json=payload)

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == ("PROFILE_AGENT_IDEMPOTENCY_CONFLICT")
    assert "traceback" not in conflict.text.casefold()

    invalid_week = parse_payload("invalid-week")
    invalid_week["current_week"] = "2026-07-14"
    invalid = client.post("/api/v1/profile-agent/parse", json=invalid_week)
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


def test_out_of_scope_blocks_before_provider_and_creates_no_draft(
    profile_agent_client: TestClient,
) -> None:
    client = profile_agent_client
    attempts = client.get("/api/v1/model-gateway/metrics").json()[
        "provider_attempts_total"
    ]
    payload = parse_payload("scope-block")
    payload["user_message"] = "我胸痛，请给我术后恢复训练处方"
    blocked = client.post("/api/v1/profile-agent/parse", json=payload)
    metrics = client.get("/api/v1/model-gateway/metrics").json()

    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "PROFILE_REQUEST_OUT_OF_SCOPE"
    request_id = blocked.json()["error"]["request_id"]
    traces = client.get(f"/api/v1/profile-agent/requests/{request_id}/traces")
    assert traces.status_code == 200
    assert traces.json()[0]["scope_guard_blocked"] is True
    assert traces.json()[0]["provider_name"] == "deterministic-scope-guard"
    assert metrics["provider_attempts_total"] == attempts
    assert metrics["scope_guard_blocks"] == 1
    assert client.get("/api/v1/plans").json() == []


def test_ambiguous_risk_returns_review_without_provider(
    profile_agent_client: TestClient,
) -> None:
    client = profile_agent_client
    attempts = client.get("/api/v1/model-gateway/metrics").json()[
        "provider_attempts_total"
    ]
    payload = parse_payload("ambiguous-risk")
    payload["user_message"] = "最近膝盖不舒服"
    response = client.post("/api/v1/profile-agent/parse", json=payload)

    assert response.status_code == 201
    assert response.json()["output"]["scope_status"] == "NEEDS_REVIEW"
    assert (
        client.get("/api/v1/model-gateway/metrics").json()["provider_attempts_total"]
        == attempts
    )


def test_gateway_disabled_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        response = client.post("/api/v1/profile-agent/parse", json=parse_payload())
    get_settings.cache_clear()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MODEL_GATEWAY_UNAVAILABLE"


def test_unreachable_http_provider_uses_explicit_template_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("MODEL_PRIMARY_PROVIDER", "http")
    monkeypatch.setenv("MODEL_PRIMARY_BASE_URL", "http://127.0.0.1:9/unreachable")
    monkeypatch.setenv("MODEL_PRIMARY_API_KEY", "local-contract-key")
    monkeypatch.setenv("MODEL_PRIMARY_MODEL", "stub-model")
    monkeypatch.setenv("MODEL_BACKUP_PROVIDER", "template-fallback")
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("MODEL_CONNECT_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        response = client.post(
            "/api/v1/profile-agent/parse",
            json=parse_payload("fallback-request"),
        )
    get_settings.cache_clear()

    assert response.status_code == 201
    assert response.json()["fallback_used"] is True
    assert response.json()["fallback_type"] == "TEMPLATE"
    assert response.json()["output"]["scope_status"] == "NEEDS_REVIEW"
    assert "local-contract-key" not in response.text
