"""Profile and constraint API behavior."""

import pytest
from fastapi.testclient import TestClient

from tests.api.helpers import profile_payload

pytestmark = pytest.mark.phase_1a


def test_profile_create_read_update_and_constraint_delete(
    api_client: TestClient,
) -> None:
    current_user = api_client.get("/api/v1/users/me")
    assert current_user.status_code == 200

    missing = api_client.get("/api/v1/profiles/me")
    assert missing.status_code == 404

    created = api_client.put("/api/v1/profiles/me", json=profile_payload())
    assert created.status_code == 200
    assert created.json()["version"] == 1
    assert created.json()["user_id"] == current_user.json()["id"]

    fetched = api_client.get("/api/v1/profiles/me")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created.json()["id"]

    update_payload = profile_payload()
    update_payload["experience_level"] = "INTERMEDIATE"
    update_payload["expected_version"] = 1
    updated = api_client.put("/api/v1/profiles/me", json=update_payload)
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    constraint = api_client.post(
        "/api/v1/profiles/me/constraints",
        json={
            "constraint_type": "AVAILABLE_EQUIPMENT",
            "constraint_value": "resistance_band",
            "priority": 100,
            "is_hard": True,
            "source": "USER_EXPLICIT",
        },
    )
    assert constraint.status_code == 201

    deleted = api_client.delete(
        f"/api/v1/profiles/me/constraints/{constraint.json()['id']}"
    )
    assert deleted.status_code == 204
    assert api_client.get("/api/v1/profiles/me").json()["constraints"] == []


def test_profile_stale_update_returns_uniform_409(api_client: TestClient) -> None:
    assert (
        api_client.put("/api/v1/profiles/me", json=profile_payload()).status_code == 200
    )
    payload = profile_payload()
    payload["experience_level"] = "INTERMEDIATE"
    payload["expected_version"] = 99

    response = api_client.put("/api/v1/profiles/me", json=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    assert "traceback" not in response.text.lower()
