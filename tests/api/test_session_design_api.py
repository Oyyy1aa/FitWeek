"""Phase 5A end-to-end API contract without external services."""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_application
from tests.api.helpers import profile_payload

pytestmark = pytest.mark.phase_5a


def payload(client_request_id: str = "session-design-1") -> dict[str, object]:
    return {
        "client_request_id": client_request_id,
        "target_date": "2026-07-20",
        "target_duration_minutes": 30,
        "location": "HOME",
        "goal": "GENERAL_FITNESS",
    }


def setup_profile(client: TestClient, *, scope: bool = True) -> None:
    assert (
        client.put(
            "/api/v1/profiles/me", json=profile_payload(scope_confirmed=scope)
        ).status_code
        == 200
    )


def test_create_get_trace_metrics_and_no_business_write(api_client: TestClient) -> None:
    setup_profile(api_client)
    created = api_client.post("/api/v1/session-designs", json=payload())
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "PENDING_REVIEW"
    assert body["source"] == "TEMPLATE_FALLBACK"
    assert body["total_seconds"] == 1800
    assert body["candidate_set_fingerprint"]
    assert body["context_fingerprint"]
    assert body["duration_policy_version"] == "session-duration-policy-v1"
    assert api_client.get(f"/api/v1/session-designs/{body['id']}").status_code == 200
    trace = api_client.get(f"/api/v1/session-designs/{body['id']}/trace")
    assert trace.status_code == 200
    assert trace.json()["model_trace_ids"]
    assert api_client.get("/api/v1/session-design/metrics").status_code == 200
    assert api_client.get("/api/v1/plans").json() == []


def test_idempotency_reuses_without_provider_and_conflicts(
    api_client: TestClient,
) -> None:
    setup_profile(api_client)
    first = api_client.post("/api/v1/session-designs", json=payload("same"))
    attempts = api_client.get("/api/v1/model-gateway/metrics").json()[
        "provider_attempts_total"
    ]
    second = api_client.post("/api/v1/session-designs", json=payload("same"))
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert (
        api_client.get("/api/v1/model-gateway/metrics").json()[
            "provider_attempts_total"
        ]
        == attempts
    )
    changed = payload("same")
    changed["target_duration_minutes"] = 15
    conflict = api_client.post("/api/v1/session-designs", json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == ("SESSION_DESIGN_IDEMPOTENCY_CONFLICT")


def test_profile_version_change_invalidates_frozen_request(
    api_client: TestClient,
) -> None:
    setup_profile(api_client)
    first = api_client.post("/api/v1/session-designs", json=payload("frozen"))
    updated_profile = profile_payload(weekly_frequency=3)
    updated_profile["expected_version"] = 1
    assert (
        api_client.put("/api/v1/profiles/me", json=updated_profile).status_code == 200
    )

    conflict = api_client.post("/api/v1/session-designs", json=payload("frozen"))

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == ("SESSION_DESIGN_IDEMPOTENCY_CONFLICT")


def test_disabled_gateway_uses_template_without_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        setup_profile(client)
        created = client.post(
            "/api/v1/session-designs", json=payload("gateway-disabled")
        )
        trace = client.get(
            f"/api/v1/session-designs/{created.json()['id']}/trace"
        ).json()
        gateway_attempts = client.get("/api/v1/model-gateway/metrics").json()[
            "provider_attempts_total"
        ]
    get_settings.cache_clear()

    assert created.status_code == 201
    assert created.json()["source"] == "TEMPLATE_FALLBACK"
    assert trace["model_trace_ids"] == []
    assert trace["provider_summary"] == "gateway-disabled:template-fallback"
    assert gateway_attempts == 0


@pytest.mark.parametrize("action", ["accept", "reject"])
def test_explicit_review_is_versioned_and_does_not_create_plan(
    api_client: TestClient, action: str
) -> None:
    setup_profile(api_client)
    draft = api_client.post(
        "/api/v1/session-designs", json=payload(f"review-{action}")
    ).json()
    reviewed = api_client.post(
        f"/api/v1/session-designs/{draft['id']}/{action}",
        json={"expected_version": 1},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == action.upper() + "ED"
    repeat = api_client.post(
        f"/api/v1/session-designs/{draft['id']}/{action}",
        json={"expected_version": 1},
    )
    assert repeat.status_code == 409
    assert api_client.get("/api/v1/plans").json() == []


def test_profile_scope_and_duration_are_hard_gates(api_client: TestClient) -> None:
    setup_profile(api_client, scope=False)
    blocked = api_client.post("/api/v1/session-designs", json=payload("scope"))
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "SCOPE_NOT_CONFIRMED"
    assert "traceback" not in blocked.text.casefold()


def test_candidate_search_failure_never_calls_provider(api_client: TestClient) -> None:
    setup_profile(api_client)
    api_client.post(
        "/api/v1/profiles/me/constraints",
        json={
            "constraint_type": "EXCLUDED_FEATURE",
            "constraint_value": "standing",
            "priority": 100,
            "is_hard": True,
            "source": "USER_EXPLICIT",
        },
    )
    before = api_client.get("/api/v1/model-gateway/metrics").json()[
        "provider_attempts_total"
    ]
    response = api_client.post("/api/v1/session-designs", json=payload("no-candidates"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SESSION_DESIGN_UNAVAILABLE"
    assert (
        api_client.get("/api/v1/model-gateway/metrics").json()[
            "provider_attempts_total"
        ]
        == before
    )


def test_missing_draft_and_write_routes_use_safe_http_semantics(
    api_client: TestClient,
) -> None:
    missing = api_client.get(
        "/api/v1/session-designs/00000000-0000-4000-8000-000000000099"
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "SESSION_DESIGN_NOT_FOUND"
    assert api_client.post("/api/v1/exercises", json={}).status_code == 405
