from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.api.helpers import generation_payload, profile_payload

pytestmark = pytest.mark.phase_6a


def _confirmed_plan(client: TestClient) -> dict[str, object]:
    assert client.put("/api/v1/profiles/me", json=profile_payload()).status_code == 200
    generated = client.post("/api/v1/plans/generate", json=generation_payload())
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed = client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _payload(
    plan: dict[str, object], *, request_id: str = "schedule-1"
) -> dict[str, object]:
    windows = []
    for day in (20, 22):
        start = datetime(2026, 7, day, 12, tzinfo=UTC)
        windows.append(
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=2)).isoformat(),
                "location": "HOME",
            }
        )
    return {
        "client_request_id": request_id,
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "timezone": "UTC",
        "availability_windows": windows,
        "manual_busy_windows": [],
    }


def test_schedule_draft_complete_review_only_flow(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    before = api_client.get("/api/v1/plans").json()
    created = api_client.post("/api/v1/schedule-drafts", json=_payload(plan))
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["outcome"] == "COMPLETE"
    assert draft["status"] == "PENDING_REVIEW"
    assert len(draft["assignments"]) == 2
    assert api_client.get(f"/api/v1/schedule-drafts/{draft['id']}").status_code == 200
    assert (
        api_client.get(f"/api/v1/schedule-drafts/{draft['id']}/trace").status_code
        == 200
    )
    busy = api_client.get(f"/api/v1/schedule-drafts/{draft['id']}/busy-snapshot")
    assert busy.status_code == 200
    assert busy.json()["mode"] == "DISABLED"
    candidates = api_client.get(f"/api/v1/schedule-drafts/{draft['id']}/candidate-set")
    assert candidates.status_code == 200
    accepted = api_client.post(
        f"/api/v1/schedule-drafts/{draft['id']}/accept",
        json={"expected_version": draft["version"]},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "ACCEPTED"
    assert api_client.get("/api/v1/plans").json() == before


def test_schedule_idempotency_reuses_without_duplicate(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    payload = _payload(plan)
    first = api_client.post("/api/v1/schedule-drafts", json=payload)
    second = api_client.post("/api/v1/schedule-drafts", json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_schedule_idempotency_conflict(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    payload = _payload(plan)
    assert api_client.post("/api/v1/schedule-drafts", json=payload).status_code == 201
    changed = deepcopy(payload)
    changed["availability_windows"][0]["end"] = "2026-07-20T15:00:00+00:00"
    conflict = api_client.post("/api/v1/schedule-drafts", json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "SCHEDULE_DRAFT_IDEMPOTENCY_CONFLICT"


def test_manual_busy_is_excluded(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    payload = _payload(plan)
    payload["manual_busy_windows"] = [
        {
            "start": "2026-07-20T12:00:00+00:00",
            "end": "2026-07-20T13:00:00+00:00",
        }
    ]
    created = api_client.post("/api/v1/schedule-drafts", json=payload)
    assert created.status_code == 201
    starts = {item["scheduled_start"] for item in created.json()["assignments"]}
    assert "2026-07-20T12:00:00Z" not in starts


def test_overlapping_same_location_availability_is_normalized(
    api_client: TestClient,
) -> None:
    plan = _confirmed_plan(api_client)
    payload = _payload(plan)
    payload["availability_windows"].append(
        {
            "start": "2026-07-20T13:00:00+00:00",
            "end": "2026-07-20T15:00:00+00:00",
            "location": "HOME",
        }
    )
    response = api_client.post("/api/v1/schedule-drafts", json=payload)
    assert response.status_code == 201, response.text


def test_old_plan_version_conflict(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    payload = _payload(plan)
    payload["expected_plan_version"] = 1
    response = api_client.post("/api/v1/schedule-drafts", json=payload)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCHEDULE_PLAN_VERSION_CONFLICT"


def test_timezone_offset_mismatch_is_safe_422(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    payload = _payload(plan)
    payload["timezone"] = "Asia/Shanghai"
    response = api_client.post("/api/v1/schedule-drafts", json=payload)
    assert response.status_code == 422
    assert "traceback" not in response.text.lower()


def test_reject_draft(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    draft = api_client.post("/api/v1/schedule-drafts", json=_payload(plan)).json()
    rejected = api_client.post(
        f"/api/v1/schedule-drafts/{draft['id']}/reject",
        json={"expected_version": draft["version"]},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"


def test_no_calendar_or_plan_write_routes_are_exposed(api_client: TestClient) -> None:
    assert api_client.post("/api/v1/calendar/events", json={}).status_code == 404
    assert api_client.post(
        "/api/v1/schedule-drafts/anything/apply", json={}
    ).status_code in {404, 422}
