"""Controlled exercise catalog API behavior."""

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.phase_1a


def test_catalog_is_read_only_unique_and_hides_disabled(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/exercises")
    assert response.status_code == 200
    exercises = response.json()
    ids = [item["id"] for item in exercises]
    assert len(ids) == 23
    assert len(ids) == len(set(ids))
    assert "burpee" not in ids

    single = api_client.get("/api/v1/exercises/bodyweight_squat")
    assert single.status_code == 200
    assert single.json()["status"] == "ACTIVE"
    assert api_client.get("/api/v1/exercises/burpee").status_code == 404

    write_attempt = api_client.post("/api/v1/exercises", json={})
    assert write_attempt.status_code == 405
    assert write_attempt.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert (
        api_client.patch("/api/v1/exercises/bodyweight_squat", json={}).status_code
        == 405
    )
    assert api_client.delete("/api/v1/exercises/bodyweight_squat").status_code == 405


def test_invalid_query_uses_uniform_error_envelope(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/exercises", params={"location": "MOON"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert "traceback" not in response.text.lower()


def test_catalog_filters_location_and_equipment(api_client: TestClient) -> None:
    response = api_client.get(
        "/api/v1/exercises",
        params=[("location", "HOME"), ("equipment", "resistance_band")],
    )
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()}
    assert "resistance_band_row" in ids
    assert "dumbbell_row" not in ids
