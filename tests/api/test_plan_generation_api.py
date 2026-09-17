"""HTTP contract for the deterministic rule-driven plan generator."""

import pytest
from fastapi.testclient import TestClient

from tests.api.helpers import generation_payload, plan_payload, profile_payload

pytestmark = pytest.mark.phase_1a1


def add_constraint(
    client: TestClient,
    constraint_type: str,
    value: str,
) -> None:
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


def test_generate_returns_201_and_is_deterministic(api_client: TestClient) -> None:
    assert (
        api_client.put("/api/v1/profiles/me", json=profile_payload()).status_code == 200
    )
    add_constraint(api_client, "EXCLUDED_FEATURE", "jumping")
    payload = generation_payload()

    first = api_client.post("/api/v1/plans/generate", json=payload)
    second = api_client.post("/api/v1/plans/generate", json=payload)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert first.json()["plan"]["status"] == "VALIDATED"
    assert first.json()["validation"] == {"passed": True, "violations": []}
    assert first.json()["generation"]["generation_policy_version"] == ("phase-1a1-v1")
    assert len(api_client.get("/api/v1/plans").json()) == 1


def test_generated_plan_can_be_queried_and_confirmed(api_client: TestClient) -> None:
    api_client.put("/api/v1/profiles/me", json=profile_payload())

    generated = api_client.post(
        "/api/v1/plans/generate", json=generation_payload()
    ).json()
    plan_id = generated["plan"]["id"]
    session_id = generated["plan"]["sessions"][0]["id"]

    assert api_client.get(f"/api/v1/plans/{plan_id}").status_code == 200
    assert api_client.get(f"/api/v1/plans/{plan_id}/sessions").status_code == 200
    assert api_client.get(f"/api/v1/sessions/{session_id}").status_code == 200
    confirmed = api_client.post(
        f"/api/v1/plans/{plan_id}/confirm",
        json={"expected_version": 1},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "CONFIRMED"


def test_insufficient_slots_return_422_without_saving(api_client: TestClient) -> None:
    api_client.put("/api/v1/profiles/me", json=profile_payload())

    response = api_client.post(
        "/api/v1/plans/generate",
        json=generation_payload(slot_count=1),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PLAN_GENERATION_FAILED"
    assert "INSUFFICIENT_AVAILABILITY" in {
        item["code"] for item in response.json()["error"]["reasons"]
    }
    assert "traceback" not in response.text.casefold()
    assert api_client.get("/api/v1/plans").json() == []


def test_missing_equipment_for_remaining_strength_actions_returns_422(
    api_client: TestClient,
) -> None:
    api_client.put(
        "/api/v1/profiles/me",
        json=profile_payload(primary_goal="BASIC_STRENGTH"),
    )
    add_constraint(api_client, "EXCLUDED_FEATURE", "bodyweight")

    response = api_client.post(
        "/api/v1/plans/generate",
        json=generation_payload(),
    )

    assert response.status_code == 422
    assert "NO_ELIGIBLE_EXERCISES" in {
        item["code"] for item in response.json()["error"]["reasons"]
    }
    assert api_client.get("/api/v1/plans").json() == []


def test_all_candidates_excluded_returns_422_without_saving(
    api_client: TestClient,
) -> None:
    api_client.put("/api/v1/profiles/me", json=profile_payload())
    for feature in ("standing", "bodyweight", "seated"):
        add_constraint(api_client, "EXCLUDED_FEATURE", feature)

    response = api_client.post(
        "/api/v1/plans/generate",
        json=generation_payload(),
    )

    assert response.status_code == 422
    assert "NO_ELIGIBLE_EXERCISES" in {
        item["code"] for item in response.json()["error"]["reasons"]
    }
    assert api_client.get("/api/v1/plans").json() == []


def test_unconfirmed_scope_returns_422(api_client: TestClient) -> None:
    api_client.put(
        "/api/v1/profiles/me",
        json=profile_payload(scope_confirmed=False),
    )

    response = api_client.post(
        "/api/v1/plans/generate",
        json=generation_payload(),
    )

    assert response.status_code == 422
    assert {item["code"] for item in response.json()["error"]["reasons"]} == {
        "SCOPE_NOT_CONFIRMED"
    }


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(week_start="2026-07-21"),
        lambda value: value["availability_slots"][0].update(
            start="2026-07-20T10:00:00"
        ),
        lambda value: value["availability_slots"].append(
            dict(value["availability_slots"][0])
        ),
    ],
)
def test_invalid_generation_request_returns_uniform_422(
    api_client: TestClient,
    mutator,
) -> None:
    api_client.put("/api/v1/profiles/me", json=profile_payload())
    payload = generation_payload()
    mutator(payload)

    response = api_client.post("/api/v1/plans/generate", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert "traceback" not in response.text.casefold()


def test_manual_structured_plan_endpoint_remains_available(
    api_client: TestClient,
) -> None:
    api_client.put("/api/v1/profiles/me", json=profile_payload())

    response = api_client.post("/api/v1/plans", json=plan_payload())

    assert response.status_code == 201
    assert response.json()["plan"]["generation_metadata"] is None
