"""Phase 7B HTTP contracts for safe Recovery application and orchestration."""

import time
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.domain.tools.enums import ToolCaller, ToolId
from app.main import create_application
from tests.api.helpers import generation_payload, profile_payload

pytestmark = pytest.mark.phase_7b


@pytest.fixture
def orchestrator_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "true")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        yield client
    get_settings.cache_clear()


def _confirmed_plan(client: TestClient, *, frequency: int = 2) -> dict[str, Any]:
    assert (
        client.put(
            "/api/v1/profiles/me",
            json=profile_payload(weekly_frequency=frequency),
        ).status_code
        == 200
    )
    payload = generation_payload(slot_count=frequency)
    generated = client.post("/api/v1/plans/generate", json=payload)
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed = client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _accepted_recovery(
    client: TestClient,
    plan: dict[str, Any],
    *,
    request_type: str,
    request_id: str,
    target_session_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "client_request_id": request_id,
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "request_type": request_type,
        "target_session_ids": [target_session_id] if target_session_id else None,
        "user_request": "Apply a controlled future-week recovery adjustment.",
    }
    created = client.post("/api/v1/recovery-drafts", json=payload)
    assert created.status_code == 201, created.text
    assert created.json()["outcome"] in {"COMPLETE", "NO_CHANGE"}
    accepted = client.post(
        f"/api/v1/recovery-drafts/{created.json()['id']}/accept",
        json={"expected_version": created.json()["version"]},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


def _apply_payload(
    plan: dict[str, Any], draft: dict[str, Any], *, request_id: str
) -> dict[str, Any]:
    return {
        "client_request_id": request_id,
        "expected_draft_version": draft["version"],
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "selected_action_candidate_ids": draft["selected_action_candidate_ids"],
    }


def _accept_children(
    client: TestClient, draft_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/recovery-drafts/{draft_id}/subdrafts", json=payload
    )
    assert response.status_code == 200, response.text
    value = response.json()
    for child_id in value["session_design_draft_ids"]:
        child = client.get(f"/api/v1/session-designs/{child_id}").json()
        reviewed = client.post(
            f"/api/v1/session-designs/{child_id}/accept",
            json={"expected_version": child["version"]},
        )
        assert reviewed.status_code == 200, reviewed.text
    for child_id in value["schedule_draft_ids"]:
        child = client.get(f"/api/v1/schedule-drafts/{child_id}").json()
        reviewed = client.post(
            f"/api/v1/schedule-drafts/{child_id}/accept",
            json={"expected_version": child["version"]},
        )
        assert reviewed.status_code == 200, reviewed.text
    return value


def _poll_run(client: TestClient, run_id: str, expected: set[str]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for _ in range(200):
        response = client.get(f"/api/v1/recovery-application-runs/{run_id}")
        assert response.status_code == 200, response.text
        value = response.json()
        if value["status"] in expected | {"FAILED_PERMANENT", "CANCELLED"}:
            return value
        time.sleep(0.01)
    raise AssertionError(f"Recovery Run did not reach {expected}: {value}")


def _seed_completed_behavior_history(client: TestClient) -> None:
    sessions: list[dict[str, object]] = []
    for offset in range(3):
        start = datetime(2026, 7, 13 + offset, 8, tzinfo=UTC)
        sessions.append(
            {
                "scheduled_start": start.isoformat(),
                "scheduled_end": (start + timedelta(minutes=30)).isoformat(),
                "location_type": "HOME",
                "session_type": "MIXED",
                "estimated_minutes": 30,
                "target_difficulty": 3,
                "exercises": [
                    {
                        "exercise_id": "bodyweight_squat",
                        "sequence_no": 1,
                        "sets": 2,
                        "repetitions": 8,
                        "duration_seconds": None,
                        "rest_seconds": 30,
                    }
                ],
            }
        )
    created = client.post(
        "/api/v1/plans",
        json={"week_start": "2026-07-13", "revision": 1, "sessions": sessions},
    )
    assert created.status_code == 201, created.text
    plan = created.json()["plan"]
    confirmed = client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    for index, session in enumerate(confirmed.json()["sessions"]):
        checked = client.post(
            f"/api/v1/sessions/{session['id']}/check-ins",
            json={
                "client_event_id": f"behavior-history-{index}",
                "status": "COMPLETED",
                "actual_minutes": 30,
                "perceived_effort": 5,
                "note": None,
                "occurred_at": (
                    datetime(2026, 7, 13 + index, 8, 30, tzinfo=UTC).isoformat()
                ),
            },
        )
        assert checked.status_code == 201, checked.text


@pytest.mark.phase_8a
def test_preview_is_read_only_and_rejects_unreviewed_or_stale(
    api_client: TestClient,
) -> None:
    plan = _confirmed_plan(api_client)
    target = plan["sessions"][1]["id"]
    draft = _accepted_recovery(
        api_client,
        plan,
        request_type="RESCHEDULE_REQUEST",
        request_id="preview",
        target_session_id=target,
    )
    payload = _apply_payload(plan, draft, request_id="preview-apply")
    before_plans = deepcopy(api_client.get("/api/v1/plans").json())
    before_draft = deepcopy(
        api_client.get(f"/api/v1/recovery-drafts/{draft['id']}").json()
    )
    container = api_client.app.state.business_container
    spacing_adapter = container.tool_gateway.registry.get(
        ToolId.RECOVERY_SPACING_VALIDATOR.value, "phase-8a-v1"
    ).adapter
    spacing_before = spacing_adapter.invocation_count
    first = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/apply-preview", json=payload
    )
    second = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/apply-preview", json=payload
    )
    assert first.status_code == second.status_code == 200
    assert spacing_adapter.invocation_count - spacing_before == 2
    assert first.json() == second.json()
    assert first.json()["requires_schedule_draft"] is True
    assert api_client.get("/api/v1/plans").json() == before_plans
    assert (
        api_client.get(f"/api/v1/recovery-drafts/{draft['id']}").json() == before_draft
    )
    stale = deepcopy(payload)
    stale["expected_draft_version"] -= 1
    response = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/apply-preview", json=stale
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RECOVERY_DRAFT_VERSION_CONFLICT"


@pytest.mark.phase_8a
def test_keep_no_change_creates_result_without_revision_and_is_idempotent(
    api_client: TestClient,
) -> None:
    plan = _confirmed_plan(api_client)
    draft = _accepted_recovery(
        api_client,
        plan,
        request_type="GENERAL_RECOVERY_REVIEW",
        request_id="keep",
    )
    payload = _apply_payload(plan, draft, request_id="keep-apply")
    before = len(api_client.get("/api/v1/plans").json())
    container = api_client.app.state.business_container
    spacing_adapter = container.tool_gateway.registry.get(
        ToolId.RECOVERY_SPACING_VALIDATOR.value, "phase-8a-v1"
    ).adapter
    spacing_before = spacing_adapter.invocation_count
    first = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/apply", json=payload
    )
    second = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/apply", json=payload
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert spacing_adapter.invocation_count - spacing_before == 1
    assert first.json()["result"]["id"] == second.json()["result"]["id"]
    assert first.json()["result"]["outcome"] == "NO_CHANGE"
    assert first.json()["result"]["created_revision"] is None
    assert first.json()["plan"] is None
    assert first.json()["draft"]["status"] == "APPLIED"
    assert len(api_client.get("/api/v1/plans").json()) == before
    conflict = deepcopy(payload)
    conflict["expected_plan_version"] += 1
    response = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/apply", json=conflict
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RECOVERY_APPLY_IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize(
    ("request_type", "child_key", "changed_field"),
    [
        ("RESCHEDULE_REQUEST", "schedule_draft_ids", "scheduled_start"),
        ("REDUCE_FUTURE_LOAD", "session_design_draft_ids", "exercises"),
    ],
)
@pytest.mark.phase_8a
def test_reviewed_subdraft_builds_one_validated_revision_only(
    api_client: TestClient,
    request_type: str,
    child_key: str,
    changed_field: str,
) -> None:
    plan = _confirmed_plan(api_client)
    target = plan["sessions"][1]["id"]
    draft = _accepted_recovery(
        api_client,
        plan,
        request_type=request_type,
        request_id=f"child-{request_type}",
        target_session_id=target,
    )
    payload = _apply_payload(plan, draft, request_id=f"apply-{request_type}")
    blocked = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/apply", json=payload
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "RECOVERY_SUBDRAFT_REVIEW_REQUIRED"
    children = _accept_children(api_client, draft["id"], payload)
    assert len(children[child_key]) >= 1
    reused = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/subdrafts", json=payload
    )
    assert reused.status_code == 200
    assert reused.json()[child_key] == children[child_key]
    applied = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/apply", json=payload
    )
    assert applied.status_code == 201, applied.text
    revision = applied.json()["plan"]
    assert revision["revision"] == 2
    assert revision["status"] == "VALIDATED"
    original = {item["id"]: item for item in plan["sessions"]}
    changed = {item["id"]: item for item in revision["sessions"]}
    assert changed[target][changed_field] != original[target][changed_field]
    for session_id in original.keys() - {target}:
        assert changed[session_id] == original[session_id]
    confirmed = api_client.post(
        f"/api/v1/plans/{plan['root_plan_id'] or plan['id']}/revisions/2/confirm",
        json={"expected_version": revision["version"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["is_current_revision"] is True
    old = api_client.get(
        f"/api/v1/plans/{plan['root_plan_id'] or plan['id']}/revisions/1"
    )
    assert old.status_code == 200


def test_remove_uses_new_revision_and_atomic_failure_leaves_no_partial_state(
    api_client: TestClient,
) -> None:
    plan = _confirmed_plan(api_client, frequency=3)
    target = plan["sessions"][-1]["id"]
    draft = _accepted_recovery(
        api_client,
        plan,
        request_type="REMOVE_FUTURE_SESSION",
        request_id="remove",
        target_session_id=target,
    )
    payload = _apply_payload(plan, draft, request_id="remove-apply")
    container = api_client.app.state.business_container
    container.recovery_application_repository.fail_next_commit_for_test()
    before = len(api_client.get("/api/v1/plans").json())
    failed = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/apply", json=payload
    )
    assert failed.status_code == 500
    assert "traceback" not in failed.text.casefold()
    assert len(api_client.get("/api/v1/plans").json()) == before
    assert (
        api_client.get(f"/api/v1/recovery-drafts/{draft['id']}").json()["status"]
        == "ACCEPTED"
    )
    applied = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/apply", json=payload
    )
    assert applied.status_code == 201
    assert len(applied.json()["plan"]["sessions"]) == 2
    assert len(api_client.get("/api/v1/plans").json()) == before + 1


def test_recovery_orchestrator_waits_confirms_and_records_safe_audit(
    orchestrator_client: TestClient,
) -> None:
    api_client = orchestrator_client
    plan = _confirmed_plan(api_client)
    target = plan["sessions"][1]["id"]
    draft = _accepted_recovery(
        api_client,
        plan,
        request_type="RESCHEDULE_REQUEST",
        request_id="run-draft",
        target_session_id=target,
    )
    request = {
        "client_request_id": "run-1",
        "recovery_draft_id": draft["id"],
        "expected_draft_version": draft["version"],
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": 1,
        "expected_plan_version": plan["version"],
    }
    created = api_client.post("/api/v1/recovery-application-runs", json=request)
    assert created.status_code == 202
    run_id = created.json()["id"]
    waiting = _poll_run(api_client, run_id, {"WAITING_SUBDRAFT_REVIEW"})
    assert waiting["status"] == "WAITING_SUBDRAFT_REVIEW"
    children = api_client.get(
        f"/api/v1/recovery-application-runs/{run_id}/subdrafts"
    ).json()
    child_id = children["schedule_draft_ids"][0]
    child = api_client.get(f"/api/v1/schedule-drafts/{child_id}").json()
    assert (
        api_client.post(
            f"/api/v1/schedule-drafts/{child_id}/accept",
            json={"expected_version": child["version"]},
        ).status_code
        == 200
    )
    assert (
        api_client.post(
            f"/api/v1/recovery-application-runs/{run_id}/continue"
        ).status_code
        == 202
    )
    waiting = _poll_run(api_client, run_id, {"WAITING_CONFIRMATION"})
    assert waiting["status"] == "WAITING_CONFIRMATION"
    steps = api_client.get(f"/api/v1/recovery-application-runs/{run_id}/steps").json()
    confirmation = next(
        item
        for item in steps
        if item["step_type"] == "WAIT_FOR_RECOVERY_REVISION_CONFIRMATION"
    )
    output = confirmation["output_payload"]
    assert (
        api_client.post(
            f"/api/v1/recovery-application-runs/{run_id}/confirm",
            json={
                "expected_revision": output["created_revision"],
                "expected_plan_version": output["resulting_plan_version"],
            },
        ).status_code
        == 202
    )
    assert _poll_run(api_client, run_id, {"COMPLETED"})["status"] == "COMPLETED"
    audits = api_client.get(f"/api/v1/recovery-application-runs/{run_id}/audit").json()
    event_types = {item["event_type"] for item in audits}
    assert {
        "RECOVERY_APPLICATION_RUN_CREATED",
        "RECOVERY_DRAFT_VALIDATED",
        "RECOVERY_ACTIONS_RESOLVED",
        "RECOVERY_WAITING_SUBDRAFT_REVIEW",
        "RECOVERY_SUBDRAFTS_ACCEPTED",
        "RECOVERY_PLAN_REVISION_CREATED",
        "RECOVERY_PLAN_SAFETY_PASSED",
        "RECOVERY_PLAN_WAITING_CONFIRMATION",
        "RECOVERY_PLAN_REVISION_CONFIRMED",
        "RECOVERY_APPLICATION_COMPLETED",
    } <= event_types
    checkpoints = api_client.get(
        f"/api/v1/recovery-application-runs/{run_id}/checkpoints"
    ).text.casefold()
    assert "user_message" not in checkpoints
    assert "check-in note" not in checkpoints
    assert "authorization" not in checkpoints


def test_orchestrator_reuses_precommitted_result_after_worker_interruption(
    orchestrator_client: TestClient,
) -> None:
    api_client = orchestrator_client
    plan = _confirmed_plan(api_client)
    target = plan["sessions"][1]["id"]
    draft = _accepted_recovery(
        api_client,
        plan,
        request_type="RESCHEDULE_REQUEST",
        request_id="interrupt-draft",
        target_session_id=target,
    )
    request = {
        "client_request_id": "interrupt-run",
        "recovery_draft_id": draft["id"],
        "expected_draft_version": draft["version"],
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": 1,
        "expected_plan_version": plan["version"],
    }
    run = api_client.post("/api/v1/recovery-application-runs", json=request).json()
    _poll_run(api_client, run["id"], {"WAITING_SUBDRAFT_REVIEW"})
    children = api_client.get(
        f"/api/v1/recovery-application-runs/{run['id']}/subdrafts"
    ).json()
    child_id = children["schedule_draft_ids"][0]
    child = api_client.get(f"/api/v1/schedule-drafts/{child_id}").json()
    api_client.post(
        f"/api/v1/schedule-drafts/{child_id}/accept",
        json={"expected_version": child["version"]},
    )
    direct = _apply_payload(plan, draft, request_id=f"recovery-run:{run['id']}")
    first = api_client.post(f"/api/v1/recovery-drafts/{draft['id']}/apply", json=direct)
    assert first.status_code == 201
    assert (
        api_client.post(
            f"/api/v1/recovery-application-runs/{run['id']}/continue"
        ).status_code
        == 202
    )
    _poll_run(api_client, run["id"], {"WAITING_CONFIRMATION"})
    revisions = api_client.get(
        f"/api/v1/plans/{plan['root_plan_id'] or plan['id']}/revisions"
    ).json()
    assert [item["plan"]["revision"] for item in revisions].count(2) == 1


def test_orchestrator_resumes_finalize_after_confirmation_worker_interruption(
    orchestrator_client: TestClient,
) -> None:
    api_client = orchestrator_client
    plan = _confirmed_plan(api_client)
    target = plan["sessions"][1]["id"]
    draft = _accepted_recovery(
        api_client,
        plan,
        request_type="RESCHEDULE_REQUEST",
        request_id="confirm-interrupt-draft",
        target_session_id=target,
    )
    request = {
        "client_request_id": "confirm-interrupt-run",
        "recovery_draft_id": draft["id"],
        "expected_draft_version": draft["version"],
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": 1,
        "expected_plan_version": plan["version"],
    }
    run = api_client.post("/api/v1/recovery-application-runs", json=request).json()
    _poll_run(api_client, run["id"], {"WAITING_SUBDRAFT_REVIEW"})
    children = api_client.get(
        f"/api/v1/recovery-application-runs/{run['id']}/subdrafts"
    ).json()
    child_id = children["schedule_draft_ids"][0]
    child = api_client.get(f"/api/v1/schedule-drafts/{child_id}").json()
    assert (
        api_client.post(
            f"/api/v1/schedule-drafts/{child_id}/accept",
            json={"expected_version": child["version"]},
        ).status_code
        == 200
    )
    api_client.post(f"/api/v1/recovery-application-runs/{run['id']}/continue")
    _poll_run(api_client, run["id"], {"WAITING_CONFIRMATION"})
    steps = api_client.get(
        f"/api/v1/recovery-application-runs/{run['id']}/steps"
    ).json()
    waiting = next(
        item
        for item in steps
        if item["step_type"] == "WAIT_FOR_RECOVERY_REVISION_CONFIRMATION"
    )
    output = waiting["output_payload"]

    pool = api_client.app.state.business_container.orchestrator_pool
    assert pool is not None
    assert api_client.portal is not None
    api_client.portal.call(pool.stop)
    confirmed = api_client.post(
        f"/api/v1/recovery-application-runs/{run['id']}/confirm",
        json={
            "expected_revision": output["created_revision"],
            "expected_plan_version": output["resulting_plan_version"],
        },
    )
    assert confirmed.status_code == 202
    assert confirmed.json()["status"] in {
        "WAITING_CONFIRMATION",
        "FINALIZING_RECOVERY_APPLICATION",
    }

    api_client.portal.call(pool.start)
    assert _poll_run(api_client, run["id"], {"COMPLETED"})["status"] == "COMPLETED"
    replay = api_client.post(
        f"/api/v1/recovery-application-runs/{run['id']}/confirm",
        json={
            "expected_revision": output["created_revision"],
            "expected_plan_version": output["resulting_plan_version"],
        },
    )
    assert replay.status_code == 202
    assert replay.json()["status"] == "COMPLETED"


def test_no_change_orchestrator_completes_without_confirmation(
    orchestrator_client: TestClient,
) -> None:
    api_client = orchestrator_client
    plan = _confirmed_plan(api_client)
    draft = _accepted_recovery(
        api_client,
        plan,
        request_type="GENERAL_RECOVERY_REVIEW",
        request_id="no-change-run-draft",
    )
    request = {
        "client_request_id": "no-change-run",
        "recovery_draft_id": draft["id"],
        "expected_draft_version": draft["version"],
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": 1,
        "expected_plan_version": plan["version"],
    }
    first = api_client.post("/api/v1/recovery-application-runs", json=request)
    assert first.status_code == 202
    assert (
        _poll_run(api_client, first.json()["id"], {"COMPLETED"})["status"]
        == "COMPLETED"
    )
    second = api_client.post("/api/v1/recovery-application-runs", json=request)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


@pytest.mark.phase_8a
def test_behavior_proposal_requires_candidate_review_before_context_use(
    api_client: TestClient,
) -> None:
    assert (
        api_client.put(
            "/api/v1/profiles/me", json=profile_payload(weekly_frequency=3)
        ).status_code
        == 200
    )
    _seed_completed_behavior_history(api_client)
    generated = api_client.post(
        "/api/v1/plans/generate", json=generation_payload(slot_count=3)
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed_response = api_client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed_response.status_code == 200, confirmed_response.text
    confirmed = confirmed_response.json()
    draft = _accepted_recovery(
        api_client,
        confirmed,
        request_type="GENERAL_RECOVERY_REVIEW",
        request_id="behavior-memory",
    )
    proposals = api_client.get(
        f"/api/v1/recovery-drafts/{draft['id']}/memory-proposals"
    ).json()
    assert len(proposals) >= 2
    selected = [proposals[0]["id"]]
    candidates_before = api_client.get("/api/v1/memory-candidates").json()
    preview = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/memory-proposals/preview",
        json={"selected_proposal_ids": selected},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["items"][0]["importable"] is True
    assert api_client.get("/api/v1/memory-candidates").json() == candidates_before

    payload = {
        "client_request_id": "behavior-import",
        "expected_draft_version": draft["version"],
        "selected_proposal_ids": selected,
    }
    container = api_client.app.state.business_container
    candidate_adapter = container.tool_gateway.registry.get(
        ToolId.MEMORY_CANDIDATE_CREATE.value, "phase-8a-v1"
    ).adapter
    candidate_before = candidate_adapter.invocation_count
    imported = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/memory-proposals/import",
        json=payload,
    )
    assert imported.status_code == 201, imported.text
    assert candidate_adapter.invocation_count - candidate_before == 1
    memory_trace = tuple(
        item
        for item in container.tool_gateway.traces.list_for_user(
            container.development_user.id
        )
        if item.tool_id is ToolId.MEMORY_CANDIDATE_CREATE
    )[-1]
    assert memory_trace.caller == ToolCaller.MEMORY_COMMITTER.value
    reused = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/memory-proposals/import",
        json=payload,
    )
    assert reused.status_code == 200
    assert reused.json()["id"] == imported.json()["id"]
    candidate_id = imported.json()["memory_candidate_ids"][0]
    candidate = api_client.get(f"/api/v1/memory-candidates/{candidate_id}").json()
    assert candidate["status"] == "PENDING_REVIEW"
    assert api_client.get("/api/v1/memories").json() == []
    context_before = api_client.post(
        "/api/v1/contexts/build",
        json={
            "agent_type": "RECOVERY_AGENT",
            "current_task": {"request_type": "recovery"},
        },
    ).json()
    assert candidate["proposed_value"] not in str(context_before)

    accepted = api_client.post(
        f"/api/v1/memory-candidates/{candidate_id}/accept",
        json={
            "client_request_id": "behavior-candidate-accept",
            "expected_candidate_version": candidate["version"],
            "confirmed_value": candidate["proposed_value"],
            "valid_until": (datetime.now(UTC) + timedelta(days=20)).isoformat(),
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["memory"]["status"] == "ACTIVE"
    context_after = api_client.post(
        "/api/v1/contexts/build",
        json={
            "agent_type": "RECOVERY_AGENT",
            "current_task": {"request_type": "recovery"},
        },
    ).json()
    assert candidate["proposed_value"] in str(context_after)


def test_calendar_reconciliation_is_explicit_idempotent_and_unapproved(
    api_client: TestClient,
) -> None:
    plan = _confirmed_plan(api_client)
    target = plan["sessions"][1]["id"]
    draft = _accepted_recovery(
        api_client,
        plan,
        request_type="RESCHEDULE_REQUEST",
        request_id="calendar-recovery",
        target_session_id=target,
    )
    apply_payload = _apply_payload(plan, draft, request_id="calendar-apply")
    _accept_children(api_client, draft["id"], apply_payload)
    applied = api_client.post(
        f"/api/v1/recovery-drafts/{draft['id']}/apply", json=apply_payload
    )
    assert applied.status_code == 201, applied.text
    result = applied.json()["result"]
    revision = applied.json()["plan"]
    confirmed = api_client.post(
        f"/api/v1/plans/{result['root_plan_id']}/revisions/2/confirm",
        json={"expected_version": revision["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text

    payload = {
        "client_request_id": "recovery-calendar-draft",
        "expected_plan_version": confirmed.json()["plan"]["version"],
        "provider": "scripted",
        "calendar_id": "primary",
    }
    created = api_client.post(
        f"/api/v1/recovery-application-results/{result['id']}/calendar-operation-draft",
        json=payload,
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "PENDING_REVIEW"
    reused = api_client.post(
        f"/api/v1/recovery-application-results/{result['id']}/calendar-operation-draft",
        json=payload,
    )
    assert reused.status_code == 200
    assert reused.json()["id"] == created.json()["id"]
    assert created.json()["status"] not in {"APPROVED", "SUCCEEDED"}
