"""HTTP fixtures for Phase 5B Session Design Plan integration tests."""

from typing import Any

from fastapi.testclient import TestClient

from tests.api.helpers import generation_payload, profile_payload


def setup_phase_5b(
    client: TestClient,
    *,
    request_suffix: str = "default",
    draft_duration: int = 30,
    draft_location: str | None = None,
    accept: bool = True,
    target_index: int = 0,
) -> dict[str, Any]:
    assert client.put("/api/v1/profiles/me", json=profile_payload()).status_code == 200
    generated = client.post("/api/v1/plans/generate", json=generation_payload()).json()[
        "plan"
    ]
    confirmed = client.post(
        f"/api/v1/plans/{generated['id']}/confirm",
        json={"expected_version": generated["version"]},
    )
    assert confirmed.status_code == 200
    plan = confirmed.json()
    session = plan["sessions"][target_index]
    draft_response = client.post(
        "/api/v1/session-designs",
        json={
            "client_request_id": f"phase-5b-draft-{request_suffix}",
            "target_date": session["scheduled_start"][:10],
            "target_duration_minutes": draft_duration,
            "location": draft_location or session["location_type"],
            "goal": "GENERAL_FITNESS",
            "preferred_session_type": session["session_type"],
        },
    )
    assert draft_response.status_code == 201, draft_response.text
    draft = draft_response.json()
    if accept:
        reviewed = client.post(
            f"/api/v1/session-designs/{draft['id']}/accept",
            json={"expected_version": draft["version"]},
        )
        assert reviewed.status_code == 200, reviewed.text
        draft = reviewed.json()
    return {"plan": plan, "session": session, "draft": draft}


def apply_payload(
    setup: dict[str, Any],
    *,
    client_request_id: str = "phase-5b-apply",
    target_session_id: str | None = None,
    expected_plan_version: int | None = None,
) -> dict[str, Any]:
    plan = setup["plan"]
    draft = setup["draft"]
    return {
        "client_request_id": client_request_id,
        "expected_draft_version": draft["version"],
        "root_plan_id": plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": (
            plan["version"] if expected_plan_version is None else expected_plan_version
        ),
        "target_session_id": target_session_id or setup["session"]["id"],
    }
