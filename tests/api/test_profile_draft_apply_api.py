"""Phase 3B HTTP contracts against the real local model stub."""

import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_application

pytestmark = pytest.mark.phase_3b


def _free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


@pytest.fixture(scope="module")
def phase3b_stub_url() -> Iterator[str]:
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.stub_model_server.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/openapi.json", timeout=0.2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.03)
    else:
        process.terminate()
        pytest.fail("Phase 3B local model stub did not start")
    try:
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=5)


def _configure(monkeypatch: pytest.MonkeyPatch, stub: str, *, workers: bool) -> None:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", str(workers).lower())
    monkeypatch.setenv("ORCHESTRATOR_WORKER_COUNT", "1")
    monkeypatch.setenv("ORCHESTRATOR_POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("ORCHESTRATOR_REAPER_INTERVAL_SECONDS", "0.03")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("MODEL_PRIMARY_PROVIDER", "http")
    monkeypatch.setenv("MODEL_PRIMARY_BASE_URL", f"{stub}/profile-apply")
    monkeypatch.setenv("MODEL_PRIMARY_API_KEY", "local-test-key")
    monkeypatch.setenv("MODEL_PRIMARY_MODEL", "phase-3b-stub")
    monkeypatch.setenv("MODEL_BACKUP_PROVIDER", "template-fallback")
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()


@pytest.fixture
def draft_client(
    monkeypatch: pytest.MonkeyPatch,
    phase3b_stub_url: str,
) -> Iterator[TestClient]:
    _configure(monkeypatch, phase3b_stub_url, workers=False)
    with TestClient(create_application()) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture
def review_run_client(
    monkeypatch: pytest.MonkeyPatch,
    phase3b_stub_url: str,
) -> Iterator[TestClient]:
    _configure(monkeypatch, phase3b_stub_url, workers=True)
    with TestClient(create_application()) as client:
        yield client
    get_settings.cache_clear()


def _parse_payload(request_id: str) -> dict[str, object]:
    return {
        "client_request_id": request_id,
        "user_message": "每周三次、每次三十分钟，居家使用弹力带。",
        "current_week": "2026-07-20",
    }


def _decision(
    request_id: str = "apply-http-1",
    *,
    draft_version: int = 1,
) -> dict[str, object]:
    return {
        "client_request_id": request_id,
        "expected_draft_version": draft_version,
        "expected_profile_version": None,
        "accept_weekly_frequency": True,
        "accept_max_session_minutes": True,
        "selected_primary_goal": "GENERAL_FITNESS",
        "accepted_equipment": ["resistance_band"],
        "accepted_locations": ["HOME"],
        "accepted_hard_constraint_indexes": [0],
        "accepted_temporary_constraint_indexes": [0],
        "temporary_constraint_expirations": {"0": "2036-08-01T00:00:00Z"},
        "confirmed_experience_level": "BEGINNER",
        "confirm_scope": True,
    }


def _parse(client: TestClient, request_id: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/profile-agent/parse", json=_parse_payload(request_id)
    )
    assert response.status_code == 201
    assert response.json()["output"]["scope_status"] == "SUPPORTED"
    return response.json()


def _wait_for_status(
    client: TestClient,
    run_id: str,
    expected: str,
    *,
    timeout: float = 4,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/profile-agent/runs/{run_id}")
        assert response.status_code == 200
        if response.json()["status"] == expected:
            return response.json()
        time.sleep(0.01)
    pytest.fail(f"Profile Agent run did not reach {expected}")


def test_direct_preview_apply_and_apply_result_are_safe(
    draft_client: TestClient,
) -> None:
    draft = _parse(draft_client, "direct-1")
    draft_id = draft["id"]
    payload = _decision()

    preview = draft_client.post(
        f"/api/v1/profile-agent/drafts/{draft_id}/apply-preview",
        json=payload,
    )
    assert preview.status_code == 200
    preview_body = preview.json()
    assert preview_body["validation"]["passed"] is True
    assert len(preview_body["ignored_soft_preferences"]) == 1
    assert len(preview_body["ignored_memory_candidates"]) == 1
    assert draft_client.get("/api/v1/profiles/me").status_code == 404

    applied = draft_client.post(
        f"/api/v1/profile-agent/drafts/{draft_id}/apply",
        json=payload,
    )
    assert applied.status_code == 200
    profile = draft_client.get("/api/v1/profiles/me")
    result = draft_client.get(f"/api/v1/profile-agent/drafts/{draft_id}/apply-result")
    assert profile.status_code == result.status_code == 200
    assert profile.json()["weekly_frequency"] == 3
    assert profile.json()["max_session_minutes"] == 30
    constraints = profile.json()["constraints"]
    assert {item["constraint_type"] for item in constraints} == {
        "AVAILABLE_EQUIPMENT",
        "ALLOWED_LOCATION",
        "EXCLUDED_FEATURE",
        "UNAVAILABLE_TIME",
    }
    temporary = next(
        item for item in constraints if item["constraint_type"] == "UNAVAILABLE_TIME"
    )
    assert temporary["valid_until"] == "2036-08-01T00:00:00Z"
    assert all(item["source"] == "USER_CONFIRMED_AGENT_DRAFT" for item in constraints)
    assert result.json()["ignored_soft_preference_count"] == 1
    assert result.json()["ignored_memory_candidate_count"] == 1
    assert draft_client.get("/api/v1/plans").json() == []
    assert "traceback" not in applied.text.casefold()
    assert "local-test-key" not in applied.text


def test_direct_apply_guards_versions_expiration_and_idempotency(
    draft_client: TestClient,
) -> None:
    stale = _parse(draft_client, "direct-stale")
    stale_response = draft_client.post(
        f"/api/v1/profile-agent/drafts/{stale['id']}/apply",
        json=_decision("stale", draft_version=2),
    )
    assert stale_response.status_code == 409
    assert stale_response.json()["error"]["code"] == ("PROFILE_DRAFT_VERSION_CONFLICT")

    missing = _parse(draft_client, "direct-missing-expiry")
    missing_payload = _decision("missing-expiry")
    missing_payload["temporary_constraint_expirations"] = {}
    missing_response = draft_client.post(
        f"/api/v1/profile-agent/drafts/{missing['id']}/apply",
        json=missing_payload,
    )
    assert missing_response.status_code == 422
    assert missing_response.json()["error"]["code"] == (
        "TEMPORARY_CONSTRAINT_EXPIRATION_REQUIRED"
    )

    first = _parse(draft_client, "direct-idempotent")
    endpoint = f"/api/v1/profile-agent/drafts/{first['id']}/apply"
    payload = _decision("same-apply")
    assert draft_client.post(endpoint, json=payload).status_code == 200
    assert draft_client.post(endpoint, json=payload).status_code == 200
    payload["accepted_locations"] = []
    conflict = draft_client.post(endpoint, json=payload)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == (
        "PROFILE_DRAFT_APPLY_IDEMPOTENCY_CONFLICT"
    )


def test_rejected_draft_never_mutates_profile(draft_client: TestClient) -> None:
    draft = _parse(draft_client, "direct-reject")
    endpoint = f"/api/v1/profile-agent/drafts/{draft['id']}"
    rejected = draft_client.post(
        f"{endpoint}/reject",
        json={"client_request_id": "reject-http", "expected_draft_version": 1},
    )
    assert rejected.status_code == 200
    blocked = draft_client.post(f"{endpoint}/apply", json=_decision("after-reject"))
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "PROFILE_DRAFT_REJECTED"
    assert draft_client.get("/api/v1/profiles/me").status_code == 404
    assert draft_client.get("/api/v1/plans").json() == []


def test_orchestrator_waits_for_exact_user_decision_then_completes(
    review_run_client: TestClient,
) -> None:
    created = review_run_client.post(
        "/api/v1/profile-agent/runs",
        json=_parse_payload("run-review-1"),
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    waiting = _wait_for_status(review_run_client, run_id, "WAITING_PROFILE_REVIEW")
    time.sleep(0.05)
    assert (
        review_run_client.get(f"/api/v1/profile-agent/runs/{run_id}").json()["status"]
        == "WAITING_PROFILE_REVIEW"
    )
    draft_id = waiting["result_reference"]
    draft = review_run_client.get(f"/api/v1/profile-agent/drafts/{draft_id}")
    assert draft.status_code == 200 and draft.json()["version"] == 1

    accepted = review_run_client.post(
        f"/api/v1/profile-agent/runs/{run_id}/apply",
        json=_decision("run-apply-1"),
    )
    assert accepted.status_code == 202
    completed = _wait_for_status(review_run_client, run_id, "COMPLETED")
    result = review_run_client.get(
        f"/api/v1/profile-agent/drafts/{draft_id}/apply-result"
    ).json()
    assert completed["result_reference"] == result["id"]
    checkpoints = review_run_client.get(
        f"/api/v1/profile-agent/runs/{run_id}/checkpoints"
    ).json()
    audits = review_run_client.get(f"/api/v1/profile-agent/runs/{run_id}/audit").json()
    checkpoint_text = str(checkpoints)
    assert "apply_policy_version" in checkpoint_text
    assert "resulting_profile_version" in checkpoint_text
    assert "run-apply-1" in checkpoint_text
    assert "accepted_hard_constraint_indexes" in checkpoint_text
    event_types = {item["event_type"] for item in audits}
    assert {
        "PROFILE_DRAFT_CREATED",
        "PROFILE_DRAFT_WAITING_REVIEW",
        "PROFILE_DRAFT_APPLY_SUBMITTED",
        "PROFILE_DRAFT_APPLIED",
        "PROFILE_RUN_COMPLETED",
    }.issubset(event_types)
    step_types = {
        item["step_type"]
        for item in review_run_client.get(
            f"/api/v1/profile-agent/runs/{run_id}/steps"
        ).json()
    }
    assert step_types == {
        "PARSE_PROFILE_REQUEST",
        "WAIT_FOR_PROFILE_DRAFT_REVIEW",
        "APPLY_PROFILE_DRAFT",
        "FINALIZE_PROFILE_RUN",
    }
    assert review_run_client.get("/api/v1/plans").json() == []


def test_orchestrator_reject_and_scope_block_never_complete(
    review_run_client: TestClient,
) -> None:
    created = review_run_client.post(
        "/api/v1/profile-agent/runs",
        json=_parse_payload("run-reject-1"),
    )
    run_id = created.json()["run_id"]
    _wait_for_status(review_run_client, run_id, "WAITING_PROFILE_REVIEW")
    rejected = review_run_client.post(
        f"/api/v1/profile-agent/runs/{run_id}/reject",
        json={"client_request_id": "run-reject", "expected_draft_version": 1},
    )
    assert rejected.status_code == 202
    assert rejected.json()["status"] == "CANCELLED"

    blocked_payload = _parse_payload("run-scope-block")
    blocked_payload["user_message"] = "我胸痛，请给我术后康复训练处方"
    blocked = review_run_client.post("/api/v1/profile-agent/runs", json=blocked_payload)
    assert blocked.status_code == 202
    failed = _wait_for_status(
        review_run_client, blocked.json()["run_id"], "FAILED_PERMANENT"
    )
    assert failed["result_reference"] is None
    assert review_run_client.get("/api/v1/profiles/me").status_code == 404
    assert review_run_client.get("/api/v1/plans").json() == []
