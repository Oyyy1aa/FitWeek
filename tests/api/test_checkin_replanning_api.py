"""End-to-end HTTP contracts for check-ins, progress, and local revisions."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from tests.api.helpers import generation_payload, profile_payload

pytestmark = pytest.mark.phase_1a2


def confirmed_plan(client: TestClient, *, frequency: int = 2) -> dict:
    profile = profile_payload(weekly_frequency=frequency)
    assert client.put("/api/v1/profiles/me", json=profile).status_code == 200
    generated = client.post(
        "/api/v1/plans/generate",
        json=generation_payload(slot_count=frequency),
    ).json()["plan"]
    response = client.post(
        f"/api/v1/plans/{generated['id']}/confirm",
        json={"expected_version": generated["version"]},
    )
    assert response.status_code == 200
    return response.json()


def check_in_payload(
    *, client_event_id: str = "http-check-in-1", actual_minutes: int = 30
) -> dict:
    return {
        "client_event_id": client_event_id,
        "status": "COMPLETED",
        "actual_minutes": actual_minutes,
        "perceived_effort": 5,
        "note": "Completed as planned.",
        "occurred_at": "2026-07-20T11:00:00Z",
    }


def availability_replan_payload(
    plan: dict,
    *,
    request_id: str = "http-replan-1",
    slot_count: int = 1,
) -> dict:
    return {
        "client_request_id": request_id,
        "expected_plan_version": plan["version"],
        "change_type": "AVAILABILITY_CHANGED",
        "effective_from": "2026-07-21T00:00:00Z",
        "replacement_availability_slots": [
            {
                "start": f"2026-07-{23 + index:02d}T12:00:00Z",
                "end": f"2026-07-{23 + index:02d}T13:00:00Z",
                "location_type": "HOME",
            }
            for index in range(slot_count)
        ],
    }


def test_check_in_first_create_reuse_query_and_progress(api_client: TestClient) -> None:
    plan = confirmed_plan(api_client)
    session_id = plan["sessions"][0]["id"]
    payload = check_in_payload()

    first = api_client.post(f"/api/v1/sessions/{session_id}/check-ins", json=payload)
    second = api_client.post(f"/api/v1/sessions/{session_id}/check-ins", json=payload)
    queried = api_client.get(f"/api/v1/sessions/{session_id}/check-in")
    progress = api_client.get(f"/api/v1/plans/{plan['id']}/progress")

    assert first.status_code == 201
    assert second.status_code == 200
    assert queried.json() == first.json() == second.json()
    assert progress.status_code == 200
    assert progress.json()["completed_sessions"] == 1
    assert progress.json()["completion_rate"] == "0.5000"


def test_check_in_payload_conflict_returns_409_without_traceback(
    api_client: TestClient,
) -> None:
    plan = confirmed_plan(api_client)
    session_id = plan["sessions"][0]["id"]
    api_client.post(
        f"/api/v1/sessions/{session_id}/check-ins",
        json=check_in_payload(),
    )

    response = api_client.post(
        f"/api/v1/sessions/{session_id}/check-ins",
        json=check_in_payload(actual_minutes=20),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert "traceback" not in response.text.casefold()


def test_local_replan_preserves_completed_session_and_can_be_confirmed(
    api_client: TestClient,
) -> None:
    plan = confirmed_plan(api_client)
    first_session = plan["sessions"][0]
    second_session = plan["sessions"][1]
    api_client.post(
        f"/api/v1/sessions/{first_session['id']}/check-ins",
        json=check_in_payload(),
    )

    response = api_client.post(
        f"/api/v1/plans/{plan['id']}/replan",
        json=availability_replan_payload(plan),
    )

    assert response.status_code == 201
    revised = response.json()["plan"]
    assert revised["status"] == "VALIDATED"
    assert revised["confirmed_at"] is None
    assert revised["revision"] == 2
    assert revised["sessions"][0] == first_session
    assert revised["sessions"][1]["id"] != second_session["id"]
    assert revised["sessions"][1]["scheduled_start"] == ("2026-07-23T12:00:00Z")

    revisions = api_client.get(f"/api/v1/plans/{plan['id']}/revisions").json()
    assert [item["plan"]["revision"] for item in revisions] == [1, 2]
    assert api_client.get(f"/api/v1/plans/{plan['id']}/revisions/1").status_code == 200
    assert api_client.get(f"/api/v1/plans/{plan['id']}/revisions/2").status_code == 200

    confirmed = api_client.post(
        f"/api/v1/plans/{plan['id']}/revisions/2/confirm",
        json={"expected_version": revised["version"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["plan"]["status"] == "CONFIRMED"
    assert confirmed.json()["is_current_revision"] is True
    after_confirmation = api_client.get(f"/api/v1/plans/{plan['id']}/revisions").json()
    assert [item["is_current_revision"] for item in after_confirmation] == [
        False,
        True,
    ]
    progress = api_client.get(f"/api/v1/plans/{plan['id']}/progress")
    assert progress.json()["plan_revision"] == 2
    assert progress.json()["completed_sessions"] == 1


def test_replan_same_request_is_idempotent(api_client: TestClient) -> None:
    plan = confirmed_plan(api_client)
    payload = availability_replan_payload(plan)

    first = api_client.post(f"/api/v1/plans/{plan['id']}/replan", json=payload)
    second = api_client.post(f"/api/v1/plans/{plan['id']}/replan", json=payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["plan"] == second.json()["plan"]
    assert len(api_client.get(f"/api/v1/plans/{plan['id']}/revisions").json()) == 2


def test_replan_request_id_conflict_returns_409(api_client: TestClient) -> None:
    plan = confirmed_plan(api_client)
    payload = availability_replan_payload(plan)
    api_client.post(f"/api/v1/plans/{plan['id']}/replan", json=payload)
    changed = deepcopy(payload)
    changed["replacement_availability_slots"][0]["start"] = "2026-07-24T12:00:00Z"
    changed["replacement_availability_slots"][0]["end"] = "2026-07-24T13:00:00Z"

    response = api_client.post(f"/api/v1/plans/{plan['id']}/replan", json=changed)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_immutable_completed_session_change_returns_422_without_revision(
    api_client: TestClient,
) -> None:
    plan = confirmed_plan(api_client)
    session_id = plan["sessions"][0]["id"]
    api_client.post(
        f"/api/v1/sessions/{session_id}/check-ins",
        json=check_in_payload(),
    )
    before = len(api_client.get(f"/api/v1/plans/{plan['id']}/revisions").json())
    payload = {
        "client_request_id": "immutable-http",
        "expected_plan_version": plan["version"],
        "change_type": "SESSION_DURATION_CHANGED",
        "effective_from": "2026-07-20T00:00:00Z",
        "max_session_minutes": 15,
    }

    response = api_client.post(f"/api/v1/plans/{plan['id']}/replan", json=payload)

    assert response.status_code == 422
    assert "IMMUTABLE_SESSION_CONFLICT" in {
        item["code"] for item in response.json()["error"]["reasons"]
    }
    assert len(api_client.get(f"/api/v1/plans/{plan['id']}/revisions").json()) == before


def test_insufficient_replacement_slots_return_422_without_revision(
    api_client: TestClient,
) -> None:
    plan = confirmed_plan(api_client, frequency=3)
    before = len(api_client.get(f"/api/v1/plans/{plan['id']}/revisions").json())
    payload = availability_replan_payload(plan, request_id="few-http", slot_count=1)
    payload["effective_from"] = "2026-07-20T00:00:00Z"

    response = api_client.post(f"/api/v1/plans/{plan['id']}/replan", json=payload)

    assert response.status_code == 422
    assert "INSUFFICIENT_REPLACEMENT_SLOTS" in {
        item["code"] for item in response.json()["error"]["reasons"]
    }
    assert len(api_client.get(f"/api/v1/plans/{plan['id']}/revisions").json()) == before


def test_no_safe_replacement_returns_422_without_revision(
    api_client: TestClient,
) -> None:
    plan = confirmed_plan(api_client)
    before = len(api_client.get(f"/api/v1/plans/{plan['id']}/revisions").json())
    payload = {
        "client_request_id": "no-exercise-http",
        "expected_plan_version": plan["version"],
        "change_type": "EXCLUDED_FEATURE_CHANGED",
        "effective_from": "2026-07-20T00:00:00Z",
        "excluded_features": [
            "bodyweight",
            "standing",
            "floor_required",
            "seated",
            "low_impact",
            "outdoor",
            "overhead",
            "jumping",
            "high_impact",
            "running",
        ],
    }

    response = api_client.post(f"/api/v1/plans/{plan['id']}/replan", json=payload)

    assert response.status_code == 422
    assert "NO_SAFE_REPLACEMENT_EXERCISE" in {
        item["code"] for item in response.json()["error"]["reasons"]
    }
    assert len(api_client.get(f"/api/v1/plans/{plan['id']}/revisions").json()) == before


def test_stale_plan_version_returns_409_without_revision(
    api_client: TestClient,
) -> None:
    plan = confirmed_plan(api_client)
    payload = availability_replan_payload(plan, request_id="stale-http")
    payload["expected_plan_version"] = 1

    response = api_client.post(f"/api/v1/plans/{plan['id']}/replan", json=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PLAN_VERSION_CONFLICT"
    assert len(api_client.get(f"/api/v1/plans/{plan['id']}/revisions").json()) == 1


def test_unconfirmed_plan_check_in_is_422(api_client: TestClient) -> None:
    api_client.put("/api/v1/profiles/me", json=profile_payload())
    plan = api_client.post("/api/v1/plans/generate", json=generation_payload()).json()[
        "plan"
    ]

    response = api_client.post(
        f"/api/v1/sessions/{plan['sessions'][0]['id']}/check-ins",
        json=check_in_payload(),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SESSION_NOT_CONFIRMED"


def test_second_event_for_same_session_returns_specific_409(
    api_client: TestClient,
) -> None:
    plan = confirmed_plan(api_client)
    session_id = plan["sessions"][0]["id"]
    api_client.post(
        f"/api/v1/sessions/{session_id}/check-ins",
        json=check_in_payload(),
    )

    response = api_client.post(
        f"/api/v1/sessions/{session_id}/check-ins",
        json=check_in_payload(client_event_id="another-event"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_ALREADY_CHECKED_IN"


def test_no_affected_sessions_returns_stable_422(api_client: TestClient) -> None:
    plan = confirmed_plan(api_client)
    payload = {
        "client_request_id": "no-impact",
        "expected_plan_version": plan["version"],
        "change_type": "SESSION_DURATION_CHANGED",
        "effective_from": "2026-07-20T00:00:00Z",
        "max_session_minutes": 45,
    }

    response = api_client.post(f"/api/v1/plans/{plan['id']}/replan", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NO_AFFECTED_SESSIONS"


def test_unsupported_local_change_returns_stable_422(api_client: TestClient) -> None:
    plan = confirmed_plan(api_client)
    payload = availability_replan_payload(plan, request_id="unsupported")
    payload["change_type"] = "TRAINING_GOAL_CHANGED"

    response = api_client.post(f"/api/v1/plans/{plan['id']}/replan", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_LOCAL_CHANGE"


def test_missing_revision_returns_stable_404(api_client: TestClient) -> None:
    plan = confirmed_plan(api_client)

    response = api_client.get(f"/api/v1/plans/{plan['id']}/revisions/99")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REVISION_NOT_FOUND"
