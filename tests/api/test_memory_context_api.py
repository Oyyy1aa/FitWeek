"""Phase 4A Memory, Candidate, Context, Audit, and error HTTP contracts."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.phase_4a


def _future() -> str:
    return (datetime.now(UTC) + timedelta(days=1)).isoformat()


def _create_profile(client: TestClient) -> None:
    response = client.put(
        "/api/v1/profiles/me",
        json={
            "experience_level": "BEGINNER",
            "weekly_frequency": 3,
            "max_session_minutes": 30,
            "primary_goal": "GENERAL_FITNESS",
            "scope_confirmed": True,
            "expected_version": None,
        },
    )
    assert response.status_code == 200


def test_memory_crud_replace_idempotency_and_errors(api_client: TestClient) -> None:
    payload = {
        "client_request_id": "http-memory-1",
        "memory_type": "PREFERRED_TIME_OF_DAY",
        "key": "preferred_time_of_day",
        "value": "morning",
        "valid_until": None,
    }
    created = api_client.post("/api/v1/memories", json=payload)
    assert created.status_code == 201
    memory_id = created.json()["id"]
    assert created.json()["evidence"]
    assert api_client.post("/api/v1/memories", json=payload).status_code == 200
    conflict = api_client.post("/api/v1/memories", json={**payload, "value": "evening"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "MEMORY_IDEMPOTENCY_CONFLICT"
    assert api_client.get(f"/api/v1/memories/{memory_id}").status_code == 200
    updated = api_client.put(
        f"/api/v1/memories/{memory_id}",
        json={
            "expected_version": 1,
            "value": "early morning",
            "valid_until": _future(),
        },
    )
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert (
        api_client.put(
            f"/api/v1/memories/{memory_id}",
            json={"expected_version": 1, "value": "night", "valid_until": None},
        ).status_code
        == 409
    )
    replaced = api_client.post(
        f"/api/v1/memories/{memory_id}/replace",
        json={
            "client_request_id": "replace-http",
            "expected_version": 2,
            "value": "evening",
            "valid_until": None,
        },
    )
    assert replaced.status_code == 200
    assert replaced.json()["previous"]["status"] == "SUPERSEDED"
    new_id = replaced.json()["replacement"]["id"]
    deleted = api_client.request(
        "DELETE", f"/api/v1/memories/{new_id}", json={"expected_version": 1}
    )
    assert deleted.status_code == 200 and deleted.json()["status"] == "DELETED"
    assert (
        api_client.get(
            "/api/v1/memories/00000000-0000-4000-8000-000000000099"
        ).status_code
        == 404
    )


def test_candidate_review_and_pending_isolation(api_client: TestClient) -> None:
    _create_profile(api_client)
    candidate = api_client.post(
        "/api/v1/memory-candidates",
        json={
            "client_request_id": "candidate-http",
            "memory_type": "PREFERRED_LOCATION",
            "key": "preferred_location",
            "value": "home",
            "source": "PROFILE_AGENT_CANDIDATE",
            "source_reference": "draft:test-only",
            "evidence_summary": "Structured development-only Candidate.",
            "confidence": "0.8",
            "expires_at": _future(),
        },
    )
    assert candidate.status_code == 201
    candidate_id = candidate.json()["id"]
    before = api_client.post(
        "/api/v1/contexts/build",
        json={
            "agent_type": "PROFILE_AGENT",
            "current_task": {"request_type": "profile"},
        },
    )
    assert before.status_code == 200
    assert all(
        candidate_id not in str(section) for section in before.json()["sections"]
    )
    accepted = api_client.post(
        f"/api/v1/memory-candidates/{candidate_id}/accept",
        json={
            "client_request_id": "accept-http",
            "expected_candidate_version": 1,
            "confirmed_value": "HOME",
            "valid_until": _future(),
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["candidate"]["status"] == "ACCEPTED"
    assert accepted.json()["memory"]["status"] == "ACTIVE"
    reject_candidate = api_client.post(
        "/api/v1/memory-candidates",
        json={
            "client_request_id": "candidate-reject-http",
            "memory_type": "DISLIKED_ACTIVITY",
            "key": "disliked_activity",
            "value": "running",
            "source": "BEHAVIOR_CANDIDATE",
            "source_reference": "summary:test-only",
            "evidence_summary": "Structured development-only Candidate.",
            "confidence": "0.6",
            "expires_at": _future(),
        },
    ).json()
    rejected = api_client.post(
        f"/api/v1/memory-candidates/{reject_candidate['id']}/reject",
        json={"client_request_id": "reject-http", "expected_candidate_version": 1},
    )
    assert rejected.status_code == 200
    assert rejected.json()["candidate"]["status"] == "REJECTED"
    assert rejected.json()["memory"] is None


def test_context_build_audit_conflict_budget_and_no_traceback(
    api_client: TestClient,
) -> None:
    _create_profile(api_client)
    api_client.post(
        "/api/v1/profiles/me/constraints",
        json={
            "constraint_type": "ALLOWED_LOCATION",
            "constraint_value": "GYM",
            "priority": 100,
            "is_hard": True,
            "source": "USER_EXPLICIT",
            "valid_until": None,
        },
    )
    api_client.post(
        "/api/v1/memories",
        json={
            "client_request_id": "context-http-memory",
            "memory_type": "PREFERRED_LOCATION",
            "key": "preferred_location",
            "value": "HOME",
            "valid_until": None,
        },
    )
    built = api_client.post(
        "/api/v1/contexts/build",
        json={
            "agent_type": "PLAN_GENERATION",
            "current_task": {"preferred_time_of_day": "evening"},
            "recent_behavior_summary": ["completed_sessions=2"],
            "catalog_reference": "catalog-v1",
        },
    )
    assert built.status_code == 200
    body = built.json()
    assert body["conflicts"][0]["higher_priority_source"] == "HARD_CONSTRAINTS"
    audit = api_client.get(f"/api/v1/contexts/audits/{body['audit_id']}")
    assert audit.status_code == 200
    assert "current_task" not in audit.json()
    budget = api_client.post(
        "/api/v1/contexts/build",
        json={
            "agent_type": "PLAN_GENERATION",
            "current_task": {"request_type": "plan"},
            "max_characters": 128,
        },
    )
    assert budget.status_code == 422
    assert budget.json()["error"]["code"] == "CONTEXT_BUDGET_EXCEEDED"
    assert "traceback" not in budget.text.casefold()
    metrics = api_client.get("/api/v1/memory/metrics")
    assert metrics.status_code == 200 and metrics.json()["context_builds"] >= 2
