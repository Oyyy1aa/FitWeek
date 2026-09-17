"""Phase 7A API proves Draft review has no business-write side effects."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_current_user
from tests.api.helpers import generation_payload, profile_payload
from tests.factories import make_user

pytestmark = pytest.mark.phase_7a


def _confirmed_plan(client: TestClient) -> dict[str, object]:
    assert client.put("/api/v1/profiles/me", json=profile_payload()).status_code == 200
    generated = client.post("/api/v1/plans/generate", json=generation_payload())
    assert generated.status_code == 201, generated.text
    value = generated.json()["plan"]
    confirmed = client.post(
        f"/api/v1/plans/{value['id']}/confirm",
        json={"expected_version": value["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _payload(
    plan: dict[str, object],
    *,
    request_id: str = "recovery-1",
    request_type: str = "RESCHEDULE_REQUEST",
    target: str | None = None,
    message: str = "周六临时没空，想调整未来训练时间。",
) -> dict[str, object]:
    return {
        "client_request_id": request_id,
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "request_type": request_type,
        "target_session_ids": [target] if target else None,
        "user_request": message,
    }


def test_recovery_create_artifacts_accept_and_no_writes(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    target = plan["sessions"][1]["id"]
    plans_before = deepcopy(api_client.get("/api/v1/plans").json())
    memories_before = deepcopy(api_client.get("/api/v1/memories").json())
    created = api_client.post(
        "/api/v1/recovery-drafts", json=_payload(plan, target=target)
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["outcome"] == "COMPLETE"
    assert draft["source"] == "DETERMINISTIC_FALLBACK"
    for suffix in (
        "behavior-summary",
        "change-impact",
        "candidate-set",
        "memory-proposals",
        "trace",
    ):
        assert (
            api_client.get(
                f"/api/v1/recovery-drafts/{draft['id']}/{suffix}"
            ).status_code
            == 200
        )
    candidate_set = api_client.get(
        f"/api/v1/recovery-drafts/{draft['id']}/candidate-set"
    ).json()
    assert any(
        item["action_type"] == "REQUEST_SESSION_RESCHEDULE"
        and item["target_session_id"] == target
        for item in candidate_set["candidates"]
    )
    reviewed = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/accept",
        json={"expected_version": 1},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "ACCEPTED"
    assert api_client.get("/api/v1/plans").json() == plans_before
    assert api_client.get("/api/v1/memories").json() == memories_before


def test_idempotency_reuses_without_provider_and_conflicts(
    api_client: TestClient,
) -> None:
    plan = _confirmed_plan(api_client)
    payload = _payload(plan, request_id="same", target=plan["sessions"][1]["id"])
    first = api_client.post("/api/v1/recovery-drafts", json=payload)
    attempts = api_client.get("/api/v1/model-gateway/metrics").json()[
        "provider_attempts_total"
    ]
    second = api_client.post("/api/v1/recovery-drafts", json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert (
        api_client.get("/api/v1/model-gateway/metrics").json()[
            "provider_attempts_total"
        ]
        == attempts
    )
    changed = deepcopy(payload)
    changed["user_request"] = "different explicit request"
    conflict = api_client.post("/api/v1/recovery-drafts", json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "RECOVERY_DRAFT_IDEMPOTENCY_CONFLICT"


def test_medical_scope_blocks_before_provider(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    before = api_client.get("/api/v1/model-gateway/metrics").json()[
        "provider_attempts_total"
    ]
    blocked = api_client.post(
        "/api/v1/recovery-drafts",
        json=_payload(
            plan,
            request_id="scope",
            message="训练时胸痛并且严重头晕，应该怎么调整？",
        ),
    )
    after = api_client.get("/api/v1/model-gateway/metrics").json()[
        "provider_attempts_total"
    ]
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "RECOVERY_SCOPE_OUT_OF_SCOPE"
    assert after == before
    assert "traceback" not in blocked.text.casefold()


def test_checked_in_or_started_target_is_immutable(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    historical = plan["sessions"][0]
    check_in = api_client.post(
        f"/api/v1/sessions/{historical['id']}/check-ins",
        json={
            "client_event_id": "recovery-check-in",
            "status": "COMPLETED",
            "actual_minutes": 30,
            "perceived_effort": 5,
            "note": "PRIVATE CHECKIN NOTE",
            "occurred_at": datetime(2026, 7, 20, 11, tzinfo=UTC).isoformat(),
        },
    )
    assert check_in.status_code == 201, check_in.text
    blocked = api_client.post(
        "/api/v1/recovery-drafts",
        json=_payload(plan, request_id="immutable", target=historical["id"]),
    )
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "RECOVERY_TARGET_SESSION_IMMUTABLE"


def test_partial_draft_cannot_be_accepted(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    created = api_client.post(
        "/api/v1/recovery-drafts",
        json=_payload(
            plan,
            request_id="frequency-min",
            request_type="REMOVE_FUTURE_SESSION",
            target=plan["sessions"][1]["id"],
            message="这周工作忙，想减少一次未来训练",
        ),
    )
    assert created.status_code == 201, created.text
    assert created.json()["outcome"] == "PARTIAL"
    accepted = api_client.post(
        f"/api/v1/recovery-drafts/{created.json()['id']}/accept",
        json={"expected_version": 1},
    )
    assert accepted.status_code == 422
    assert accepted.json()["error"]["code"] == "RECOVERY_DRAFT_NOT_ACCEPTABLE"


def test_source_version_and_write_shape_are_rejected(api_client: TestClient) -> None:
    plan = _confirmed_plan(api_client)
    payload = _payload(plan, request_id="version")
    payload["expected_plan_version"] = 1
    conflict = api_client.post("/api/v1/recovery-drafts", json=payload)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "RECOVERY_PLAN_VERSION_CONFLICT"
    arbitrary = _payload(plan, request_id="patch")
    arbitrary["plan_patch"] = {"exercise_id": "invented"}
    response = api_client.post("/api/v1/recovery-drafts", json=arbitrary)
    assert response.status_code == 422


def test_needs_review_is_user_action_required_and_cannot_be_accepted(
    api_client: TestClient,
) -> None:
    plan = _confirmed_plan(api_client)
    before = api_client.get("/api/v1/model-gateway/metrics").json()[
        "provider_attempts_total"
    ]
    created = api_client.post(
        "/api/v1/recovery-drafts",
        json=_payload(
            plan,
            request_id="needs-review",
            target=plan["sessions"][1]["id"],
            message="最近训练不舒服，想调整。",
        ),
    )
    assert created.status_code == 201
    assert created.json()["outcome"] == "USER_ACTION_REQUIRED"
    assert created.json()["selected_action_candidate_ids"] == []
    assert (
        api_client.get("/api/v1/model-gateway/metrics").json()[
            "provider_attempts_total"
        ]
        == before
    )
    accepted = api_client.post(
        f"/api/v1/recovery-drafts/{created.json()['id']}/accept",
        json={"expected_version": 1},
    )
    assert accepted.status_code == 422


def test_no_change_accept_reject_terminal_user_scope_and_metrics(
    api_client: TestClient,
) -> None:
    plan = _confirmed_plan(api_client)
    no_change = api_client.post(
        "/api/v1/recovery-drafts",
        json=_payload(
            plan,
            request_id="no-change",
            request_type="GENERAL_RECOVERY_REVIEW",
            message="如果没有安全调整，请保留当前计划。",
        ),
    )
    assert no_change.status_code == 201
    assert no_change.json()["outcome"] == "NO_CHANGE"
    accepted = api_client.post(
        f"/api/v1/recovery-drafts/{no_change.json()['id']}/accept",
        json={"expected_version": 1},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "ACCEPTED"

    rejected_draft = api_client.post(
        "/api/v1/recovery-drafts",
        json=_payload(
            plan,
            request_id="reject",
            request_type="GENERAL_RECOVERY_REVIEW",
        ),
    ).json()
    rejected = api_client.post(
        f"/api/v1/recovery-drafts/{rejected_draft['id']}/reject",
        json={"expected_version": 1},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert (
        api_client.post(
            f"/api/v1/recovery-drafts/{rejected_draft['id']}/reject",
            json={"expected_version": 2},
        ).status_code
        == 409
    )

    api_client.app.dependency_overrides[get_current_user] = lambda: make_user()
    try:
        assert (
            api_client.get(
                f"/api/v1/recovery-drafts/{no_change.json()['id']}"
            ).status_code
            == 404
        )
    finally:
        api_client.app.dependency_overrides.pop(get_current_user, None)
    metrics = api_client.get("/api/v1/recovery/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["recovery_drafts_accepted"] == 1
    assert metrics.json()["recovery_drafts_rejected"] == 1


def test_behavior_window_over_56_days_is_rejected_without_traceback(
    api_client: TestClient,
) -> None:
    plan = _confirmed_plan(api_client)
    payload = _payload(plan, request_id="window")
    payload["behavior_window"] = {
        "start_date": "2026-01-01",
        "end_date": "2026-03-01",
    }
    response = api_client.post("/api/v1/recovery-drafts", json=payload)
    assert response.status_code == 422
    assert "traceback" not in response.text.casefold()
