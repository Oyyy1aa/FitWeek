"""Four-Uvicorn Phase 7B Recovery application acceptance."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.api.helpers import profile_payload

pytestmark = pytest.mark.phase_7b


def _free_port() -> int:
    with closing(socket.socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _wait_http(url: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=0.25).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.04)
    pytest.fail(f"HTTP service did not start: {url}")


def _port_is_free(port: int) -> bool:
    with closing(socket.socket()) as candidate:
        candidate.settimeout(0.2)
        return candidate.connect_ex(("127.0.0.1", port)) != 0


def _monday(offset_weeks: int = 0) -> date:
    today = datetime.now(UTC).date()
    current = today - timedelta(days=today.weekday())
    return current + timedelta(weeks=offset_weeks)


def _generation_payload(week_start: date, frequency: int) -> dict[str, object]:
    offsets = (0, 2, 4, 5, 6)[:frequency]
    return {
        "week_start": week_start.isoformat(),
        "availability_slots": [
            {
                "start": datetime.combine(
                    week_start + timedelta(days=offset),
                    datetime.min.time(),
                    tzinfo=UTC,
                )
                .replace(hour=10)
                .isoformat(),
                "end": datetime.combine(
                    week_start + timedelta(days=offset),
                    datetime.min.time(),
                    tzinfo=UTC,
                )
                .replace(hour=11)
                .isoformat(),
                "location_type": "HOME",
            }
            for offset in offsets
        ],
        "preferred_locations": ["HOME"],
        "preferred_session_types": [],
    }


def _historical_payload(week_start: date) -> dict[str, object]:
    sessions: list[dict[str, object]] = []
    for offset in range(3):
        start = datetime.combine(
            week_start + timedelta(days=offset),
            datetime.min.time(),
            tzinfo=UTC,
        ).replace(hour=8)
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
    return {"week_start": week_start.isoformat(), "revision": 1, "sessions": sessions}


def _confirmed_generated(
    client: httpx.Client, week_start: date, frequency: int
) -> dict[str, Any]:
    generated = client.post(
        "/api/v1/plans/generate",
        json=_generation_payload(week_start, frequency),
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed = client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _accepted_recovery(
    client: httpx.Client,
    plan: dict[str, Any],
    *,
    request_id: str,
    request_type: str,
    target: str | None,
) -> dict[str, Any]:
    created = client.post(
        "/api/v1/recovery-drafts",
        json={
            "client_request_id": request_id,
            "root_plan_id": plan["root_plan_id"] or plan["id"],
            "source_revision": plan["revision"],
            "expected_plan_version": plan["version"],
            "request_type": request_type,
            "target_session_ids": [target] if target else None,
            "user_request": "Controlled Recovery application request.",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["outcome"] in {"COMPLETE", "NO_CHANGE"}
    accepted = client.post(
        f"/api/v1/recovery-drafts/{created.json()['id']}/accept",
        json={"expected_version": created.json()["version"]},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


def _apply_payload(
    plan: dict[str, Any], draft: dict[str, Any], request_id: str
) -> dict[str, object]:
    return {
        "client_request_id": request_id,
        "expected_draft_version": draft["version"],
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "selected_action_candidate_ids": draft["selected_action_candidate_ids"],
    }


def _accept_children(
    client: httpx.Client, draft_id: str, payload: dict[str, object]
) -> dict[str, Any]:
    children = client.post(
        f"/api/v1/recovery-drafts/{draft_id}/subdrafts", json=payload
    )
    assert children.status_code == 200, children.text
    value = children.json()
    for child_id in value["session_design_draft_ids"]:
        child = client.get(f"/api/v1/session-designs/{child_id}").json()
        accepted = client.post(
            f"/api/v1/session-designs/{child_id}/accept",
            json={"expected_version": child["version"]},
        )
        assert accepted.status_code == 200, accepted.text
    for child_id in value["schedule_draft_ids"]:
        child = client.get(f"/api/v1/schedule-drafts/{child_id}").json()
        accepted = client.post(
            f"/api/v1/schedule-drafts/{child_id}/accept",
            json={"expected_version": child["version"]},
        )
        assert accepted.status_code == 200, accepted.text
    return value


def _poll(client: httpx.Client, run_id: str, expected: set[str]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for _ in range(300):
        response = client.get(f"/api/v1/recovery-application-runs/{run_id}")
        assert response.status_code == 200, response.text
        value = response.json()
        if value["status"] in expected | {"FAILED_PERMANENT", "CANCELLED"}:
            return value
        time.sleep(0.02)
    pytest.fail(f"Recovery Run did not reach {expected}: {value}")


def _start_server(
    module: str, port: int, log: object, *, environment: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=environment,
        stdout=log,  # type: ignore[arg-type]
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_real_http_recovery_application_memory_and_calendar(tmp_path: Path) -> None:
    model_port, read_port, write_port, api_port = (_free_port() for _ in range(4))
    paths = {
        name: tmp_path / f"phase-7b-{name}.log"
        for name in ("model", "calendar-read", "calendar-write", "api")
    }
    environment = os.environ.copy()
    environment.update(
        {
            "PERSISTENCE_BACKEND": "memory",
            "REDIS_ENABLED": "false",
            "ORCHESTRATOR_ENABLED": "true",
            "MODEL_GATEWAY_ENABLED": "true",
            "MODEL_PRIMARY_PROVIDER": "http",
            "MODEL_PRIMARY_BASE_URL": f"http://127.0.0.1:{model_port}",
            "MODEL_PRIMARY_API_KEY": "local-test-key",
            "MODEL_PRIMARY_MODEL": "phase-7b-local-stub",
            "MODEL_BACKUP_PROVIDER": "template-fallback",
            "CALENDAR_READ_ENABLED": "true",
            "CALENDAR_READ_PROVIDER": "http",
            "CALENDAR_READ_BASE_URL": f"http://127.0.0.1:{read_port}",
            "CALENDAR_READ_API_KEY": "local-calendar-read-test-key",
            "CALENDAR_WRITE_ENABLED": "true",
            "CALENDAR_WRITE_PROVIDER": "http",
            "CALENDAR_WRITE_BASE_URL": f"http://127.0.0.1:{write_port}",
            "CALENDAR_WRITE_API_KEY": "local-calendar-write-test-key",
            "CONTEXT_DEBUG_API_ENABLED": "false",
        }
    )
    opened = {name: path.open("w", encoding="utf-8") for name, path in paths.items()}
    processes: list[subprocess.Popen[bytes]] = []
    try:
        processes = [
            _start_server(
                "tests.stub_model_server.app:app", model_port, opened["model"]
            ),
            _start_server(
                "tests.stub_calendar_server.app:app", read_port, opened["calendar-read"]
            ),
            _start_server(
                "tests.stub_calendar_server.app:app",
                write_port,
                opened["calendar-write"],
            ),
            _start_server(
                "app.main:app", api_port, opened["api"], environment=environment
            ),
        ]
        for port in (model_port, read_port, write_port):
            _wait_http(f"http://127.0.0.1:{port}/openapi.json")
        _wait_http(f"http://127.0.0.1:{api_port}/health/live")
        with (
            httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=10) as client,
            httpx.Client(base_url=f"http://127.0.0.1:{model_port}", timeout=2) as model,
            httpx.Client(
                base_url=f"http://127.0.0.1:{write_port}", timeout=2
            ) as calendar_write,
        ):
            assert (
                client.put(
                    "/api/v1/profiles/me", json=profile_payload(weekly_frequency=3)
                ).status_code
                == 200
            )

            historical = client.post(
                "/api/v1/plans", json=_historical_payload(_monday(-1))
            )
            assert historical.status_code == 201, historical.text
            history_plan = historical.json()["plan"]
            history_plan = client.post(
                f"/api/v1/plans/{history_plan['id']}/confirm",
                json={"expected_version": history_plan["version"]},
            ).json()
            for index, session in enumerate(history_plan["sessions"]):
                checked = client.post(
                    f"/api/v1/sessions/{session['id']}/check-ins",
                    json={
                        "client_event_id": f"phase-7b-history-{index}",
                        "status": "SKIPPED",
                        "actual_minutes": 0,
                        "perceived_effort": None,
                        "note": "PHASE7B_PRIVATE_CHECKIN_NOTE",
                        "occurred_at": session["scheduled_end"],
                    },
                )
                assert checked.status_code == 201, checked.text

            # Reschedule through the full Orchestrator review/confirm path.
            reschedule_plan = _confirmed_generated(client, _monday(1), 3)
            target = reschedule_plan["sessions"][1]["id"]
            reschedule = _accepted_recovery(
                client,
                reschedule_plan,
                request_id="http-reschedule-draft",
                request_type="RESCHEDULE_REQUEST",
                target=target,
            )
            run_request = {
                "client_request_id": "http-reschedule-run",
                "recovery_draft_id": reschedule["id"],
                "expected_draft_version": reschedule["version"],
                "root_plan_id": reschedule_plan["root_plan_id"]
                or reschedule_plan["id"],
                "source_revision": 1,
                "expected_plan_version": reschedule_plan["version"],
            }
            run = client.post(
                "/api/v1/recovery-application-runs", json=run_request
            ).json()
            assert (
                _poll(client, run["id"], {"WAITING_SUBDRAFT_REVIEW"})["status"]
                == "WAITING_SUBDRAFT_REVIEW"
            )
            children = client.get(
                f"/api/v1/recovery-application-runs/{run['id']}/subdrafts"
            ).json()
            assert len(children["schedule_draft_ids"]) == 1
            child_id = children["schedule_draft_ids"][0]
            child = client.get(f"/api/v1/schedule-drafts/{child_id}").json()
            assert (
                client.post(
                    f"/api/v1/schedule-drafts/{child_id}/accept",
                    json={"expected_version": child["version"]},
                ).status_code
                == 200
            )
            attempts_after_subdraft = client.get(
                "/api/v1/model-gateway/metrics"
            ).json()["provider_attempts_total"]
            assert (
                client.post(
                    f"/api/v1/recovery-application-runs/{run['id']}/continue"
                ).status_code
                == 202
            )
            waiting = _poll(client, run["id"], {"WAITING_CONFIRMATION"})
            assert waiting["status"] == "WAITING_CONFIRMATION"
            revisions = client.get(
                f"/api/v1/plans/{run_request['root_plan_id']}/revisions"
            ).json()
            revision_two = next(
                item["plan"] for item in revisions if item["plan"]["revision"] == 2
            )
            assert revision_two["status"] == "VALIDATED"
            assert (
                client.post(
                    f"/api/v1/recovery-application-runs/{run['id']}/confirm",
                    json={
                        "expected_revision": 2,
                        "expected_plan_version": revision_two["version"],
                    },
                ).status_code
                == 202
            )
            assert _poll(client, run["id"], {"COMPLETED"})["status"] == "COMPLETED"
            assert (
                client.get("/api/v1/model-gateway/metrics").json()[
                    "provider_attempts_total"
                ]
                == attempts_after_subdraft
            )
            application = client.get(
                f"/api/v1/recovery-drafts/{reschedule['id']}/application-result"
            ).json()
            assert (
                client.get(
                    f"/api/v1/plans/{run_request['root_plan_id']}/revisions/1"
                ).status_code
                == 200
            )

            # Calendar reconciliation is a separate, explicit, still-unapproved Draft.
            current = client.get(
                f"/api/v1/plans/{run_request['root_plan_id']}/revisions/2"
            ).json()
            operation = client.post(
                "/api/v1/recovery-application-results/"
                f"{application['result']['id']}/calendar-operation-draft",
                json={
                    "client_request_id": "http-recovery-calendar",
                    "expected_plan_version": current["plan"]["version"],
                    "provider": "http",
                    "calendar_id": "primary",
                },
            )
            assert operation.status_code == 201, operation.text
            assert operation.json()["status"] == "PENDING_REVIEW"
            assert calendar_write.get("/admin/events").json()["event_count"] == 0

            # Redesign uses the frozen Session Design child once and preserves schedule.
            redesign_plan = _confirmed_generated(client, _monday(2), 3)
            redesign_target = redesign_plan["sessions"][1]["id"]
            redesign = _accepted_recovery(
                client,
                redesign_plan,
                request_id="http-redesign-draft",
                request_type="REDUCE_FUTURE_LOAD",
                target=redesign_target,
            )
            redesign_payload = _apply_payload(
                redesign_plan, redesign, "http-redesign-apply"
            )
            redesign_children = _accept_children(
                client, redesign["id"], redesign_payload
            )
            assert len(redesign_children["session_design_draft_ids"]) == 1
            applied_redesign = client.post(
                f"/api/v1/recovery-drafts/{redesign['id']}/apply",
                json=redesign_payload,
            )
            assert applied_redesign.status_code == 201, applied_redesign.text
            original_session = next(
                item
                for item in redesign_plan["sessions"]
                if item["id"] == redesign_target
            )
            changed_session = next(
                item
                for item in applied_redesign.json()["plan"]["sessions"]
                if item["id"] == redesign_target
            )
            assert (
                changed_session["scheduled_start"]
                == original_session["scheduled_start"]
            )
            assert changed_session["location_type"] == original_session["location_type"]
            assert changed_session["exercises"] != original_session["exercises"]

            # Repeated skip evidence allows a controlled combined local adjustment.
            combined_plan = _confirmed_generated(client, _monday(3), 3)
            combined_target = combined_plan["sessions"][1]["id"]
            combined = _accepted_recovery(
                client,
                combined_plan,
                request_id="http-combined-draft",
                request_type="GENERAL_RECOVERY_REVIEW",
                target=combined_target,
            )
            combined_payload = _apply_payload(
                combined_plan, combined, "http-combined-apply"
            )
            combined_children = _accept_children(
                client, combined["id"], combined_payload
            )
            assert len(combined_children["session_design_draft_ids"]) == 1
            assert len(combined_children["schedule_draft_ids"]) == 1
            combined_apply = client.post(
                f"/api/v1/recovery-drafts/{combined['id']}/apply",
                json=combined_payload,
            )
            assert combined_apply.status_code == 201, combined_apply.text
            before = next(
                item
                for item in combined_plan["sessions"]
                if item["id"] == combined_target
            )
            after = next(
                item
                for item in combined_apply.json()["plan"]["sessions"]
                if item["id"] == combined_target
            )
            assert after["scheduled_start"] != before["scheduled_start"]
            assert after["exercises"] != before["exercises"]

            # Frozen Proposal becomes pending, then ACTIVE only after Accept.
            proposals = client.get(
                f"/api/v1/recovery-drafts/{combined['id']}/memory-proposals"
            ).json()
            selected = [proposals[0]["id"]]
            memory_preview = client.post(
                f"/api/v1/recovery-drafts/{combined['id']}/memory-proposals/preview",
                json={"selected_proposal_ids": selected},
            )
            assert memory_preview.status_code == 200
            imported = client.post(
                f"/api/v1/recovery-drafts/{combined['id']}/memory-proposals/import",
                json={
                    "client_request_id": "http-behavior-import",
                    "expected_draft_version": combined_apply.json()["draft"]["version"],
                    "selected_proposal_ids": selected,
                },
            )
            assert imported.status_code == 201, imported.text
            candidate_id = imported.json()["memory_candidate_ids"][0]
            candidate = client.get(f"/api/v1/memory-candidates/{candidate_id}").json()
            assert candidate["status"] == "PENDING_REVIEW"
            assert client.get("/api/v1/memories").json() == []
            accepted_memory = client.post(
                f"/api/v1/memory-candidates/{candidate_id}/accept",
                json={
                    "client_request_id": "http-behavior-accept",
                    "expected_candidate_version": candidate["version"],
                    "confirmed_value": candidate["proposed_value"],
                    "valid_until": (datetime.now(UTC) + timedelta(days=20)).isoformat(),
                },
            )
            assert accepted_memory.status_code == 200, accepted_memory.text
            assert accepted_memory.json()["memory"]["status"] == "ACTIVE"

            # Model fallback proves NO_CHANGE creates no meaningless Revision.
            assert model.post("/admin/scenario/server-error").status_code == 200
            no_change_plan = _confirmed_generated(client, _monday(4), 3)
            no_change = _accepted_recovery(
                client,
                no_change_plan,
                request_id="http-no-change-draft",
                request_type="GENERAL_RECOVERY_REVIEW",
                target=None,
            )
            assert no_change["outcome"] == "NO_CHANGE"
            no_change_apply = client.post(
                f"/api/v1/recovery-drafts/{no_change['id']}/apply",
                json=_apply_payload(no_change_plan, no_change, "http-no-change-apply"),
            )
            assert no_change_apply.status_code == 201
            assert no_change_apply.json()["plan"] is None
            assert no_change_apply.json()["result"]["outcome"] == "NO_CHANGE"
            assert model.post("/admin/scenario/success").status_code == 200

            # Simulate a worker crash after Apply committed but before Step success.
            # The resumed run must reuse the same result and the same Revision.
            interrupted_plan = _confirmed_generated(client, _monday(5), 3)
            interrupted_target = interrupted_plan["sessions"][1]["id"]
            interrupted = _accepted_recovery(
                client,
                interrupted_plan,
                request_id="http-interrupted-draft",
                request_type="RESCHEDULE_REQUEST",
                target=interrupted_target,
            )
            interrupted_run_request = {
                "client_request_id": "http-interrupted-run",
                "recovery_draft_id": interrupted["id"],
                "expected_draft_version": interrupted["version"],
                "root_plan_id": (
                    interrupted_plan["root_plan_id"] or interrupted_plan["id"]
                ),
                "source_revision": 1,
                "expected_plan_version": interrupted_plan["version"],
            }
            interrupted_run = client.post(
                "/api/v1/recovery-application-runs",
                json=interrupted_run_request,
            ).json()
            _poll(
                client,
                interrupted_run["id"],
                {"WAITING_SUBDRAFT_REVIEW"},
            )
            interrupted_children = client.get(
                f"/api/v1/recovery-application-runs/{interrupted_run['id']}/subdrafts"
            ).json()
            interrupted_child_id = interrupted_children["schedule_draft_ids"][0]
            interrupted_child = client.get(
                f"/api/v1/schedule-drafts/{interrupted_child_id}"
            ).json()
            assert (
                client.post(
                    f"/api/v1/schedule-drafts/{interrupted_child_id}/accept",
                    json={"expected_version": interrupted_child["version"]},
                ).status_code
                == 200
            )
            precommitted = client.post(
                f"/api/v1/recovery-drafts/{interrupted['id']}/apply",
                json=_apply_payload(
                    interrupted_plan,
                    interrupted,
                    f"recovery-run:{interrupted_run['id']}",
                ),
            )
            assert precommitted.status_code == 201, precommitted.text
            assert (
                client.post(
                    "/api/v1/recovery-application-runs/"
                    f"{interrupted_run['id']}/continue"
                ).status_code
                == 202
            )
            interrupted_waiting = _poll(
                client,
                interrupted_run["id"],
                {"WAITING_CONFIRMATION"},
            )
            assert interrupted_waiting["status"] == "WAITING_CONFIRMATION"
            interrupted_revisions = client.get(
                f"/api/v1/plans/{interrupted_run_request['root_plan_id']}/revisions"
            ).json()
            assert (
                sum(item["plan"]["revision"] == 2 for item in interrupted_revisions)
                == 1
            )

            # A pending Draft and stale Plan version remain blocked without traceback.
            pending_plan = _confirmed_generated(client, _monday(6), 3)
            pending = client.post(
                "/api/v1/recovery-drafts",
                json={
                    "client_request_id": "http-pending",
                    "root_plan_id": pending_plan["root_plan_id"] or pending_plan["id"],
                    "source_revision": 1,
                    "expected_plan_version": pending_plan["version"],
                    "request_type": "RESCHEDULE_REQUEST",
                    "target_session_ids": [pending_plan["sessions"][1]["id"]],
                    "user_request": "Controlled pending request.",
                },
            ).json()
            blocked_payload = _apply_payload(
                pending_plan, pending, "http-pending-apply"
            )
            blocked = client.post(
                f"/api/v1/recovery-drafts/{pending['id']}/apply-preview",
                json=blocked_payload,
            )
            assert blocked.status_code in {409, 422}
            assert "traceback" not in blocked.text.casefold()
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=6)
        for handle in opened.values():
            handle.close()

    combined_logs = "\n".join(
        path.read_text(encoding="utf-8") for path in paths.values()
    )
    for prohibited in (
        "local-test-key",
        "local-calendar-read-test-key",
        "local-calendar-write-test-key",
        "Authorization",
        "PHASE7B_PRIVATE_CHECKIN_NOTE",
        "refresh_token",
        "traceback",
    ):
        assert prohibited.casefold() not in combined_logs.casefold()
    assert all(
        _port_is_free(port) for port in (model_port, read_port, write_port, api_port)
    )
