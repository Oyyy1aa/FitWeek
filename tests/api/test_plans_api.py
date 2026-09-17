"""Weekly plan, confirmation, and read-only session API behavior."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api.helpers import plan_payload, profile_payload

pytestmark = pytest.mark.phase_1a


def _create_profile_and_constraints(client: TestClient) -> None:
    assert client.put("/api/v1/profiles/me", json=profile_payload()).status_code == 200
    for constraint_type, value in (
        ("AVAILABLE_EQUIPMENT", "resistance_band"),
        ("EXCLUDED_FEATURE", "jumping"),
    ):
        response = client.post(
            "/api/v1/profiles/me/constraints",
            json={
                "constraint_type": constraint_type,
                "constraint_value": value,
                "priority": 100,
                "is_hard": True,
                "source": "USER_EXPLICIT",
            },
        )
        assert response.status_code == 201


def test_complete_plan_and_session_http_flow(api_client: TestClient) -> None:
    _create_profile_and_constraints(api_client)
    catalog = api_client.get(
        "/api/v1/exercises",
        params=[("location", "HOME"), ("equipment", "resistance_band")],
    )
    assert catalog.status_code == 200
    assert any(item["id"] == "resistance_band_row" for item in catalog.json())

    created = api_client.post(
        "/api/v1/plans",
        json=plan_payload("resistance_band_row"),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["validation"] == {"passed": True, "violations": []}
    plan_id = body["plan"]["id"]
    session_id = body["plan"]["sessions"][0]["id"]

    assert api_client.get("/api/v1/plans").status_code == 200
    assert api_client.get(f"/api/v1/plans/{plan_id}").status_code == 200

    confirmed = api_client.post(
        f"/api/v1/plans/{plan_id}/confirm",
        json={"expected_version": 1},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "CONFIRMED"
    assert confirmed.json()["version"] == 2

    sessions = api_client.get(f"/api/v1/plans/{plan_id}/sessions")
    assert sessions.status_code == 200
    assert len(sessions.json()) == 2
    assert api_client.get(f"/api/v1/sessions/{session_id}").status_code == 200

    stale = api_client.post(
        f"/api/v1/plans/{plan_id}/confirm",
        json={"expected_version": 1},
    )
    assert stale.status_code == 409
    assert "traceback" not in stale.text.lower()


@pytest.mark.parametrize(
    ("exercise_id", "violation_code"),
    [
        ("does_not_exist", "EXERCISE_NOT_FOUND"),
        ("dumbbell_row", "EQUIPMENT_MISMATCH"),
        ("jumping_jack", "EXCLUDED_FEATURE"),
    ],
)
def test_invalid_plans_return_422_and_are_not_saved(
    api_client: TestClient,
    exercise_id: str,
    violation_code: str,
) -> None:
    _create_profile_and_constraints(api_client)

    response = api_client.post("/api/v1/plans", json=plan_payload(exercise_id))

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "PLAN_SAFETY_VALIDATION_FAILED"
    assert violation_code in {item["code"] for item in error["violations"]}
    assert "traceback" not in response.text.lower()
    assert api_client.get("/api/v1/plans").json() == []


def test_missing_plan_and_session_return_404(api_client: TestClient) -> None:
    missing = uuid4()
    assert api_client.get(f"/api/v1/plans/{missing}").status_code == 404
    assert api_client.get(f"/api/v1/sessions/{missing}").status_code == 404


def test_scope_not_confirmed_returns_422(api_client: TestClient) -> None:
    assert (
        api_client.put(
            "/api/v1/profiles/me",
            json=profile_payload(scope_confirmed=False),
        ).status_code
        == 200
    )

    response = api_client.post("/api/v1/plans", json=plan_payload())

    assert response.status_code == 422
    assert "SCOPE_NOT_CONFIRMED" in {
        item["code"] for item in response.json()["error"]["violations"]
    }
