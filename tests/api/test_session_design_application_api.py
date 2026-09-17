"""Phase 5B direct Apply and application-Run HTTP contracts."""

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_current_user
from app.config import get_settings
from app.main import create_application
from tests.factories import make_user
from tests.phase_5b_helpers import apply_payload, setup_phase_5b

pytestmark = pytest.mark.phase_5b


@pytest.fixture
def phase_5b_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "true")
    monkeypatch.setenv("ORCHESTRATOR_WORKER_COUNT", "2")
    monkeypatch.setenv("ORCHESTRATOR_POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("ORCHESTRATOR_REAPER_INTERVAL_SECONDS", "0.03")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        yield client
    get_settings.cache_clear()


def _wait(
    client: TestClient,
    run_id: str,
    status: str,
    *,
    timeout: float = 3,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/session-design-application-runs/{run_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] == status:
            return body
        if body["status"] == "FAILED_PERMANENT":
            pytest.fail(f"Run failed permanently: {body}")
        time.sleep(0.01)
    pytest.fail(f"Run did not reach {status}.")


def test_preview_is_stable_and_read_only(api_client: TestClient) -> None:
    setup = setup_phase_5b(api_client, request_suffix="preview")
    payload = apply_payload(setup, client_request_id="preview-only")
    before_revisions = api_client.get(
        f"/api/v1/plans/{setup['plan']['id']}/revisions"
    ).json()
    first = api_client.post(
        f"/api/v1/session-designs/{setup['draft']['id']}/apply-preview",
        json=payload,
    )
    second = api_client.post(
        f"/api/v1/session-designs/{setup['draft']['id']}/apply-preview",
        json=payload,
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    body = first.json()
    assert body["validation"] == {"passed": True, "violations": []}
    assert body["changed_session_ids"] == [setup["session"]["id"]]
    assert body["target_session_before"]["session_id"] == setup["session"]["id"]
    assert body["target_session_after"]["session_id"] == setup["session"]["id"]
    assert (
        api_client.get(f"/api/v1/plans/{setup['plan']['id']}/revisions").json()
        == before_revisions
    )
    draft = api_client.get(f"/api/v1/session-designs/{setup['draft']['id']}").json()
    assert draft["status"] == "ACCEPTED"
    assert draft["version"] == setup["draft"]["version"]


def test_other_user_session_design_resources_are_hidden(
    api_client: TestClient,
) -> None:
    setup = setup_phase_5b(api_client, request_suffix="other-user-http")
    api_client.app.dependency_overrides[get_current_user] = make_user
    try:
        response = api_client.post(
            f"/api/v1/session-designs/{setup['draft']['id']}/apply-preview",
            json=apply_payload(setup, client_request_id="other-user-preview"),
        )
    finally:
        api_client.app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 404
    assert "traceback" not in response.text.casefold()


def test_apply_creates_only_validated_target_revision_and_is_idempotent(
    api_client: TestClient,
) -> None:
    setup = setup_phase_5b(api_client, request_suffix="apply")
    payload = apply_payload(setup, client_request_id="apply-idempotent")
    attempts_before = api_client.get("/api/v1/model-gateway/metrics").json()[
        "provider_attempts_total"
    ]
    created = api_client.post(
        f"/api/v1/session-designs/{setup['draft']['id']}/apply", json=payload
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["created"] is True
    assert body["plan"]["revision"] == 2
    assert body["plan"]["status"] == "VALIDATED"
    assert body["draft"]["status"] == "APPLIED"
    assert body["plan"]["parent_revision"] == 1
    assert body["plan"]["revision_reason"] == "SESSION_DESIGN_APPLIED"
    old_sessions = setup["plan"]["sessions"]
    new_sessions = body["plan"]["sessions"]
    target_id = setup["session"]["id"]
    for old, new in zip(old_sessions, new_sessions, strict=True):
        if old["id"] == target_id:
            assert new["id"] == old["id"]
            assert new["scheduled_start"] == old["scheduled_start"]
            assert new["scheduled_end"] == old["scheduled_end"]
            assert new["location_type"] == old["location_type"]
            assert new["session_type"] == old["session_type"]
            assert new["version"] == old["version"] + 1
            assert new["exercises"] != old["exercises"]
        else:
            assert new == old
    revisions = api_client.get(f"/api/v1/plans/{setup['plan']['id']}/revisions").json()
    assert len(revisions) == 2
    assert revisions[0]["is_current_revision"] is True
    assert revisions[1]["is_current_revision"] is False
    reused = api_client.post(
        f"/api/v1/session-designs/{setup['draft']['id']}/apply", json=payload
    )
    assert reused.status_code == 200
    assert reused.json()["result"]["id"] == body["result"]["id"]
    assert reused.json()["plan"]["id"] == body["plan"]["id"]
    assert (
        api_client.get("/api/v1/model-gateway/metrics").json()[
            "provider_attempts_total"
        ]
        == attempts_before
    )
    queried = api_client.get(
        f"/api/v1/session-designs/{setup['draft']['id']}/application-result"
    )
    assert queried.status_code == 200
    assert queried.json()["result"]["id"] == body["result"]["id"]


def test_apply_idempotency_conflict_and_draft_single_use(
    api_client: TestClient,
) -> None:
    setup = setup_phase_5b(api_client, request_suffix="conflict")
    payload = apply_payload(setup, client_request_id="same-apply")
    assert (
        api_client.post(
            f"/api/v1/session-designs/{setup['draft']['id']}/apply", json=payload
        ).status_code
        == 201
    )
    changed = dict(payload)
    changed["target_session_id"] = setup["plan"]["sessions"][1]["id"]
    conflict = api_client.post(
        f"/api/v1/session-designs/{setup['draft']['id']}/apply", json=changed
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == (
        "SESSION_DESIGN_APPLY_IDEMPOTENCY_CONFLICT"
    )
    another = dict(payload)
    another["client_request_id"] = "another-apply"
    repeated_draft = api_client.post(
        f"/api/v1/session-designs/{setup['draft']['id']}/apply", json=another
    )
    assert repeated_draft.status_code == 409
    assert repeated_draft.json()["error"]["code"] == (
        "SESSION_DESIGN_DRAFT_ALREADY_APPLIED"
    )
    assert (
        len(api_client.get(f"/api/v1/plans/{setup['plan']['id']}/revisions").json())
        == 2
    )


def test_existing_pending_revision_blocks_a_second_application(
    api_client: TestClient,
) -> None:
    setup = setup_phase_5b(api_client, request_suffix="pending-revision")
    assert (
        api_client.post(
            f"/api/v1/session-designs/{setup['draft']['id']}/apply",
            json=apply_payload(setup, client_request_id="first-pending-revision"),
        ).status_code
        == 201
    )
    target = setup["session"]
    created = api_client.post(
        "/api/v1/session-designs",
        json={
            "client_request_id": "second-pending-draft",
            "target_date": target["scheduled_start"][:10],
            "target_duration_minutes": 30,
            "location": target["location_type"],
            "goal": "GENERAL_FITNESS",
            "preferred_session_type": target["session_type"],
        },
    )
    assert created.status_code == 201
    accepted = api_client.post(
        f"/api/v1/session-designs/{created.json()['id']}/accept",
        json={"expected_version": created.json()["version"]},
    )
    assert accepted.status_code == 200
    second_setup = {
        "plan": setup["plan"],
        "session": target,
        "draft": accepted.json(),
    }
    blocked = api_client.post(
        f"/api/v1/session-designs/{accepted.json()['id']}/apply",
        json=apply_payload(
            second_setup,
            client_request_id="second-pending-revision",
        ),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "SESSION_DESIGN_PLAN_VERSION_CONFLICT"
    assert (
        len(api_client.get(f"/api/v1/plans/{setup['plan']['id']}/revisions").json())
        == 2
    )


@pytest.mark.parametrize("review", ["pending", "rejected"])
def test_only_accepted_draft_can_apply(api_client: TestClient, review: str) -> None:
    setup = setup_phase_5b(api_client, request_suffix=review, accept=False)
    if review == "rejected":
        rejected = api_client.post(
            f"/api/v1/session-designs/{setup['draft']['id']}/reject",
            json={"expected_version": setup["draft"]["version"]},
        )
        assert rejected.status_code == 200
        setup["draft"] = rejected.json()
    response = api_client.post(
        f"/api/v1/session-designs/{setup['draft']['id']}/apply",
        json=apply_payload(setup, client_request_id=f"blocked-{review}"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_DESIGN_DRAFT_NOT_ACCEPTED"
    assert (
        len(api_client.get(f"/api/v1/plans/{setup['plan']['id']}/revisions").json())
        == 1
    )


def test_checked_in_session_is_a_hard_gate(
    api_client: TestClient,
) -> None:
    checked = setup_phase_5b(api_client, request_suffix="checked")
    check_in = api_client.post(
        f"/api/v1/sessions/{checked['session']['id']}/check-ins",
        json={
            "client_event_id": "phase-5b-check-in",
            "status": "COMPLETED",
            "actual_minutes": 30,
            "perceived_effort": 5,
            "note": None,
            "occurred_at": "2026-07-19T00:00:00Z",
        },
    )
    assert check_in.status_code == 201
    blocked = api_client.post(
        f"/api/v1/session-designs/{checked['draft']['id']}/apply-preview",
        json=apply_payload(checked, client_request_id="checked-apply"),
    )
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == (
        "SESSION_DESIGN_TARGET_SESSION_CHECKED_IN"
    )


def test_plan_version_is_a_hard_gate(api_client: TestClient) -> None:
    stale = setup_phase_5b(api_client, request_suffix="stale")
    stale_response = api_client.post(
        f"/api/v1/session-designs/{stale['draft']['id']}/apply-preview",
        json=apply_payload(
            stale, client_request_id="stale-plan", expected_plan_version=1
        ),
    )
    assert stale_response.status_code == 409
    assert stale_response.json()["error"]["code"] == (
        "SESSION_DESIGN_PLAN_VERSION_CONFLICT"
    )


def test_duration_and_location_mismatch_do_not_save(api_client: TestClient) -> None:
    duration = setup_phase_5b(api_client, request_suffix="duration", draft_duration=45)
    mismatch = api_client.post(
        f"/api/v1/session-designs/{duration['draft']['id']}/apply",
        json=apply_payload(duration, client_request_id="duration-mismatch"),
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == (
        "SESSION_DESIGN_TARGET_DURATION_MISMATCH"
    )
    assert (
        len(api_client.get(f"/api/v1/plans/{duration['plan']['id']}/revisions").json())
        == 1
    )


def test_location_mismatch_is_rejected(api_client: TestClient) -> None:
    location = setup_phase_5b(
        api_client,
        request_suffix="location",
        draft_location="GYM",
    )
    response = api_client.post(
        f"/api/v1/session-designs/{location['draft']['id']}/apply-preview",
        json=apply_payload(location, client_request_id="location-mismatch"),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == (
        "SESSION_DESIGN_TARGET_LOCATION_MISMATCH"
    )


def test_checked_non_target_is_immutable_and_association_survives_confirmation(
    api_client: TestClient,
) -> None:
    setup = setup_phase_5b(api_client, request_suffix="immutable", target_index=1)
    first_session = setup["plan"]["sessions"][0]
    check_in = api_client.post(
        f"/api/v1/sessions/{first_session['id']}/check-ins",
        json={
            "client_event_id": "phase-5b-preserved-check-in",
            "status": "SKIPPED",
            "actual_minutes": 0,
            "perceived_effort": None,
            "note": None,
            "occurred_at": "2026-07-19T00:00:00Z",
        },
    )
    assert check_in.status_code == 201
    payload = apply_payload(setup, client_request_id="immutable-preview")
    preview = api_client.post(
        f"/api/v1/session-designs/{setup['draft']['id']}/apply-preview",
        json=payload,
    )
    assert preview.status_code == 200
    assert preview.json()["immutable_session_ids"] == [first_session["id"]]
    applied = api_client.post(
        f"/api/v1/session-designs/{setup['draft']['id']}/apply", json=payload
    )
    assert applied.status_code == 201
    confirmed = api_client.post(
        f"/api/v1/plans/{setup['plan']['id']}/revisions/2/confirm",
        json={"expected_version": 1},
    )
    assert confirmed.status_code == 200
    stored_check_in = api_client.get(f"/api/v1/sessions/{first_session['id']}/check-in")
    assert stored_check_in.status_code == 200
    assert stored_check_in.json()["id"] == check_in.json()["id"]


def test_non_current_source_revision_is_rejected(api_client: TestClient) -> None:
    setup = setup_phase_5b(api_client, request_suffix="current-source")
    first_payload = apply_payload(setup, client_request_id="make-revision-two")
    applied = api_client.post(
        f"/api/v1/session-designs/{setup['draft']['id']}/apply",
        json=first_payload,
    )
    assert applied.status_code == 201
    assert (
        api_client.post(
            f"/api/v1/plans/{setup['plan']['id']}/revisions/2/confirm",
            json={"expected_version": 1},
        ).status_code
        == 200
    )
    second_draft = api_client.post(
        "/api/v1/session-designs",
        json={
            "client_request_id": "phase-5b-second-draft",
            "target_date": setup["session"]["scheduled_start"][:10],
            "target_duration_minutes": 30,
            "location": setup["session"]["location_type"],
            "goal": "GENERAL_FITNESS",
        },
    ).json()
    second_draft = api_client.post(
        f"/api/v1/session-designs/{second_draft['id']}/accept",
        json={"expected_version": second_draft["version"]},
    ).json()
    stale_source = {
        "client_request_id": "old-source",
        "expected_draft_version": second_draft["version"],
        "root_plan_id": setup["plan"]["id"],
        "source_revision": 1,
        "expected_plan_version": 2,
        "target_session_id": setup["session"]["id"],
    }
    rejected = api_client.post(
        f"/api/v1/session-designs/{second_draft['id']}/apply-preview",
        json=stale_source,
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == (
        "SESSION_DESIGN_SOURCE_REVISION_NOT_CURRENT"
    )


def test_orchestrator_waits_then_confirms_and_finalizes(
    phase_5b_client: TestClient,
) -> None:
    client = phase_5b_client
    setup = setup_phase_5b(client, request_suffix="run")
    payload = {
        "client_request_id": "phase-5b-run",
        "draft_id": setup["draft"]["id"],
        "root_plan_id": setup["plan"]["id"],
        "source_revision": 1,
        "expected_plan_version": setup["plan"]["version"],
        "target_session_id": setup["session"]["id"],
    }
    created = client.post("/api/v1/session-design-application-runs", json=payload)
    assert created.status_code == 202
    run_id = created.json()["id"]
    waiting = _wait(client, run_id, "WAITING_CONFIRMATION")
    assert waiting["result_reference"]
    revisions = client.get(f"/api/v1/plans/{setup['plan']['id']}/revisions").json()
    assert len(revisions) == 2
    assert revisions[1]["plan"]["status"] == "VALIDATED"
    assert revisions[0]["is_current_revision"] is True
    steps = client.get(f"/api/v1/session-design-application-runs/{run_id}/steps").json()
    wait_step = next(
        item
        for item in steps
        if item["step_type"] == "WAIT_FOR_PLAN_REVISION_CONFIRMATION"
    )
    assert wait_step["status"] == "WAITING_USER"
    confirmation = client.post(
        f"/api/v1/session-design-application-runs/{run_id}/confirm",
        json={"expected_revision": 2, "expected_plan_version": 1},
    )
    assert confirmation.status_code == 202
    completed = _wait(client, run_id, "COMPLETED")
    assert completed["result_reference"] == waiting["result_reference"]
    final_revisions = client.get(
        f"/api/v1/plans/{setup['plan']['id']}/revisions"
    ).json()
    assert final_revisions[0]["plan"]["revision"] == 1
    assert final_revisions[1]["plan"]["revision"] == 2
    assert [item["is_current_revision"] for item in final_revisions] == [False, True]
    repeated = client.post(
        f"/api/v1/session-design-application-runs/{run_id}/confirm",
        json={"expected_revision": 2, "expected_plan_version": 1},
    )
    assert repeated.status_code == 202
    assert len(client.get(f"/api/v1/plans/{setup['plan']['id']}/revisions").json()) == 2
    checkpoints = client.get(
        f"/api/v1/session-design-application-runs/{run_id}/checkpoints"
    ).json()
    forbidden = ("user_message", "raw_text", "system_prompt", "authorization")
    assert not any(token in str(checkpoints).casefold() for token in forbidden)
    audit = client.get(f"/api/v1/session-design-application-runs/{run_id}/audit").json()
    events = {item["event_type"] for item in audit}
    assert {
        "SESSION_DESIGN_APPLICATION_RUN_CREATED",
        "SESSION_DESIGN_TARGET_VALIDATED",
        "SESSION_DESIGN_PLAN_REVISION_CREATED",
        "SESSION_DESIGN_PLAN_SAFETY_PASSED",
        "SESSION_DESIGN_PLAN_WAITING_CONFIRMATION",
        "SESSION_DESIGN_PLAN_REVISION_CONFIRMED",
        "SESSION_DESIGN_APPLICATION_COMPLETED",
    }.issubset(events)


def test_run_idempotency_cancel_and_early_confirm(
    phase_5b_client: TestClient,
) -> None:
    client = phase_5b_client
    setup = setup_phase_5b(client, request_suffix="cancel")
    payload = {
        "client_request_id": "phase-5b-cancel",
        "draft_id": setup["draft"]["id"],
        "root_plan_id": setup["plan"]["id"],
        "source_revision": 1,
        "expected_plan_version": setup["plan"]["version"],
        "target_session_id": setup["session"]["id"],
    }
    first = client.post("/api/v1/session-design-application-runs", json=payload)
    second = client.post("/api/v1/session-design-application-runs", json=payload)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    run_id = first.json()["id"]
    waiting = _wait(client, run_id, "WAITING_CONFIRMATION")
    assert waiting["status"] == "WAITING_CONFIRMATION"
    cancelled = client.post(f"/api/v1/session-design-application-runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    revisions = client.get(f"/api/v1/plans/{setup['plan']['id']}/revisions").json()
    assert len(revisions) == 2
    assert revisions[1]["plan"]["status"] == "VALIDATED"
    confirm = client.post(
        f"/api/v1/session-design-application-runs/{run_id}/confirm",
        json={"expected_revision": 2, "expected_plan_version": 1},
    )
    assert confirm.status_code == 409
    assert "traceback" not in confirm.text.casefold()
