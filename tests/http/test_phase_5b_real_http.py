"""Two-Uvicorn Phase 5B acceptance flow using only the local model stub."""

import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.api.helpers import generation_payload, profile_payload

pytestmark = pytest.mark.phase_5b


def _future_generation_payload() -> dict[str, object]:
    today = datetime.now(UTC).date()
    days_until_monday = (7 - today.weekday()) % 7 or 7
    week_start = today + timedelta(days=days_until_monday)
    payload = generation_payload()
    slots = []
    for offset in (0, 2):
        start = datetime.combine(
            week_start + timedelta(days=offset),
            datetime.min.time(),
            tzinfo=UTC,
        ) + timedelta(hours=10)
        slots.append(
            {
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=60)).isoformat(),
                "location_type": "HOME",
            }
        )
    payload["week_start"] = week_start.isoformat()
    payload["availability_slots"] = slots
    return payload


def _free_port() -> int:
    with closing(socket.socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _wait_http(url: str, *, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=0.2).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.03)
    pytest.fail(f"HTTP service did not start: {url}")


def _wait_run(client: httpx.Client, run_id: str, status: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/session-design-application-runs/{run_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] == status:
            return body
        if body["status"] == "FAILED_PERMANENT":
            pytest.fail(f"Session application Run failed: {body}")
        time.sleep(0.02)
    pytest.fail(f"Run did not reach {status}.")


def _draft(
    client: httpx.Client,
    *,
    request_id: str,
    session: dict[str, Any],
    duration: int = 30,
    accept: bool = True,
) -> dict[str, Any]:
    created = client.post(
        "/api/v1/session-designs",
        json={
            "client_request_id": request_id,
            "target_date": session["scheduled_start"][:10],
            "target_duration_minutes": duration,
            "location": session["location_type"],
            "goal": "GENERAL_FITNESS",
            "preferred_session_type": session["session_type"],
        },
    )
    assert created.status_code == 201, created.text
    result = created.json()
    if accept:
        reviewed = client.post(
            f"/api/v1/session-designs/{result['id']}/accept",
            json={"expected_version": result["version"]},
        )
        assert reviewed.status_code == 200, reviewed.text
        result = reviewed.json()
    return result


def _apply_payload(
    *,
    request_id: str,
    draft: dict[str, Any],
    plan: dict[str, Any],
    session: dict[str, Any],
    source_revision: int | None = None,
    expected_plan_version: int | None = None,
) -> dict[str, Any]:
    return {
        "client_request_id": request_id,
        "expected_draft_version": draft["version"],
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": source_revision or plan["revision"],
        "expected_plan_version": expected_plan_version or plan["version"],
        "target_session_id": session["id"],
    }


def test_real_stub_and_fitweek_http_phase_5b(tmp_path: Path) -> None:
    stub_port = _free_port()
    api_port = _free_port()
    stub_log_path = tmp_path / "phase-5b-stub.log"
    api_log_path = tmp_path / "phase-5b-api.log"
    environment = os.environ.copy()
    environment.update(
        {
            "PERSISTENCE_BACKEND": "memory",
            "REDIS_ENABLED": "false",
            "ORCHESTRATOR_ENABLED": "true",
            "ORCHESTRATOR_WORKER_COUNT": "2",
            "ORCHESTRATOR_POLL_INTERVAL_SECONDS": "0.01",
            "ORCHESTRATOR_REAPER_INTERVAL_SECONDS": "0.03",
            "MODEL_GATEWAY_ENABLED": "true",
            "MODEL_PRIMARY_PROVIDER": "http",
            "MODEL_PRIMARY_BASE_URL": f"http://127.0.0.1:{stub_port}",
            "MODEL_PRIMARY_API_KEY": "local-test-key",
            "MODEL_PRIMARY_MODEL": "phase-5b-local-stub",
            "MODEL_BACKUP_PROVIDER": "template-fallback",
            "MODEL_REQUEST_TIMEOUT_SECONDS": "1",
            "CONTEXT_DEBUG_API_ENABLED": "false",
        }
    )
    with (
        stub_log_path.open("w", encoding="utf-8") as stub_log,
        api_log_path.open("w", encoding="utf-8") as api_log,
    ):
        stub = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.stub_model_server.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(stub_port),
                "--log-level",
                "warning",
            ],
            stdout=stub_log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        api = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--log-level",
                "warning",
            ],
            env=environment,
            stdout=api_log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            _wait_http(f"http://127.0.0.1:{stub_port}/openapi.json")
            _wait_http(f"http://127.0.0.1:{api_port}/health/live")
            with httpx.Client(
                base_url=f"http://127.0.0.1:{api_port}", timeout=3
            ) as client:
                assert (
                    client.put(
                        "/api/v1/profiles/me", json=profile_payload()
                    ).status_code
                    == 200
                )
                generated = client.post(
                    "/api/v1/plans/generate", json=_future_generation_payload()
                ).json()["plan"]
                confirmed = client.post(
                    f"/api/v1/plans/{generated['id']}/confirm",
                    json={"expected_version": generated["version"]},
                )
                assert confirmed.status_code == 200
                revision_one = confirmed.json()
                target = revision_one["sessions"][0]
                draft = _draft(
                    client,
                    request_id="real-http-positive-draft",
                    session=target,
                )
                attempts = client.get("/api/v1/model-gateway/metrics").json()[
                    "provider_attempts_total"
                ]
                preview_payload = _apply_payload(
                    request_id="real-http-preview",
                    draft=draft,
                    plan=revision_one,
                    session=target,
                )
                preview = client.post(
                    f"/api/v1/session-designs/{draft['id']}/apply-preview",
                    json=preview_payload,
                )
                assert preview.status_code == 200
                run_payload = {
                    "client_request_id": "real-http-application-run",
                    "draft_id": draft["id"],
                    "root_plan_id": revision_one["id"],
                    "source_revision": 1,
                    "expected_plan_version": revision_one["version"],
                    "target_session_id": target["id"],
                }
                run = client.post(
                    "/api/v1/session-design-application-runs", json=run_payload
                )
                assert run.status_code == 202
                run_id = run.json()["id"]
                waiting = _wait_run(client, run_id, "WAITING_CONFIRMATION")
                assert waiting["result_reference"]
                before_confirm = client.get(
                    f"/api/v1/plans/{revision_one['id']}/revisions"
                ).json()
                assert [item["plan"]["status"] for item in before_confirm] == [
                    "CONFIRMED",
                    "VALIDATED",
                ]
                assert (
                    client.post(
                        f"/api/v1/session-design-application-runs/{run_id}/confirm",
                        json={"expected_revision": 2, "expected_plan_version": 1},
                    ).status_code
                    == 202
                )
                _wait_run(client, run_id, "COMPLETED")
                revisions = client.get(
                    f"/api/v1/plans/{revision_one['id']}/revisions"
                ).json()
                revision_two = revisions[1]["plan"]
                assert [item["is_current_revision"] for item in revisions] == [
                    False,
                    True,
                ]
                for old, new in zip(
                    revision_one["sessions"], revision_two["sessions"], strict=True
                ):
                    if old["id"] == target["id"]:
                        assert new["exercises"] != old["exercises"]
                        for key in (
                            "id",
                            "scheduled_start",
                            "scheduled_end",
                            "location_type",
                            "session_type",
                        ):
                            assert new[key] == old[key]
                    else:
                        assert new == old
                assert (
                    client.get(f"/api/v1/session-designs/{draft['id']}").json()[
                        "status"
                    ]
                    == "APPLIED"
                )
                assert (
                    client.get(
                        f"/api/v1/session-designs/{draft['id']}/application-result"
                    ).status_code
                    == 200
                )
                assert (
                    client.get("/api/v1/model-gateway/metrics").json()[
                        "provider_attempts_total"
                    ]
                    == attempts
                )

                pending = _draft(
                    client,
                    request_id="real-http-pending",
                    session=revision_two["sessions"][1],
                    accept=False,
                )
                pending_apply = client.post(
                    f"/api/v1/session-designs/{pending['id']}/apply",
                    json=_apply_payload(
                        request_id="pending-apply",
                        draft=pending,
                        plan=revision_two,
                        session=revision_two["sessions"][1],
                    ),
                )
                assert pending_apply.status_code == 409

                repeat_applied = client.post(
                    f"/api/v1/session-designs/{draft['id']}/apply",
                    json=_apply_payload(
                        request_id="repeat-applied",
                        draft=client.get(
                            f"/api/v1/session-designs/{draft['id']}"
                        ).json(),
                        plan=revision_one,
                        session=target,
                    ),
                )
                assert repeat_applied.status_code == 409

                second = revision_two["sessions"][1]
                assert (
                    client.post(
                        f"/api/v1/sessions/{second['id']}/check-ins",
                        json={
                            "client_event_id": "real-http-check-in",
                            "status": "SKIPPED",
                            "actual_minutes": 0,
                            "perceived_effort": None,
                            "note": None,
                            "occurred_at": "2026-07-19T00:00:00Z",
                        },
                    ).status_code
                    == 201
                )
                checked_draft = _draft(
                    client,
                    request_id="real-http-checked",
                    session=second,
                )
                checked_apply = client.post(
                    f"/api/v1/session-designs/{checked_draft['id']}/apply",
                    json=_apply_payload(
                        request_id="checked-apply",
                        draft=checked_draft,
                        plan=revision_two,
                        session=second,
                    ),
                )
                assert checked_apply.status_code == 422

                long_draft = _draft(
                    client,
                    request_id="real-http-long",
                    session=revision_two["sessions"][0],
                    duration=45,
                )
                long_apply = client.post(
                    f"/api/v1/session-designs/{long_draft['id']}/apply",
                    json=_apply_payload(
                        request_id="long-apply",
                        draft=long_draft,
                        plan=revision_two,
                        session=revision_two["sessions"][0],
                    ),
                )
                assert long_apply.status_code == 422

                stale_draft = _draft(
                    client,
                    request_id="real-http-stale-source",
                    session=revision_two["sessions"][0],
                )
                stale_source = client.post(
                    f"/api/v1/session-designs/{stale_draft['id']}/apply",
                    json=_apply_payload(
                        request_id="stale-source-apply",
                        draft=stale_draft,
                        plan=revision_one,
                        session=target,
                    ),
                )
                assert stale_source.status_code == 409
                stale_version = client.post(
                    f"/api/v1/session-designs/{stale_draft['id']}/apply",
                    json=_apply_payload(
                        request_id="stale-version-apply",
                        draft=stale_draft,
                        plan=revision_two,
                        session=revision_two["sessions"][0],
                        expected_plan_version=1,
                    ),
                )
                assert stale_version.status_code == 409

                steps = client.get(
                    f"/api/v1/session-design-application-runs/{run_id}/steps"
                ).json()
                build = next(
                    item
                    for item in steps
                    if item["step_type"] == "BUILD_SESSION_PLAN_REVISION"
                )
                reused_id = build["output_payload"]["apply_client_request_id"]
                idempotency_conflict = client.post(
                    f"/api/v1/session-designs/{draft['id']}/apply",
                    json=_apply_payload(
                        request_id=reused_id,
                        draft=draft,
                        plan=revision_one,
                        session=revision_one["sessions"][1],
                    ),
                )
                assert idempotency_conflict.status_code == 409
                assert (
                    len(
                        client.get(
                            f"/api/v1/plans/{revision_one['id']}/revisions"
                        ).json()
                    )
                    == 2
                )

                safety_draft = _draft(
                    client,
                    request_id="real-http-safety-gate",
                    session=revision_two["sessions"][0],
                )
                excluded_feature = None
                for selected in safety_draft["exercises"]:
                    exercise = client.get(
                        f"/api/v1/exercises/{selected['exercise_id']}"
                    ).json()
                    if exercise["feature_tags"]:
                        excluded_feature = sorted(exercise["feature_tags"])[0]
                        break
                assert excluded_feature is not None
                assert (
                    client.post(
                        "/api/v1/profiles/me/constraints",
                        json={
                            "constraint_type": "EXCLUDED_FEATURE",
                            "constraint_value": excluded_feature,
                            "priority": 100,
                            "is_hard": True,
                            "source": "USER_EXPLICIT",
                        },
                    ).status_code
                    == 201
                )
                safety_failure = client.post(
                    f"/api/v1/session-designs/{safety_draft['id']}/apply",
                    json=_apply_payload(
                        request_id="real-http-safety-failure",
                        draft=safety_draft,
                        plan=revision_two,
                        session=revision_two["sessions"][0],
                    ),
                )
                assert safety_failure.status_code == 422
                assert (
                    len(
                        client.get(
                            f"/api/v1/plans/{revision_one['id']}/revisions"
                        ).json()
                    )
                    == 2
                )
        finally:
            for process in (api, stub):
                process.terminate()
            for process in (api, stub):
                process.wait(timeout=5)

    log_text = (
        api_log_path.read_text(encoding="utf-8")
        + stub_log_path.read_text(encoding="utf-8")
    ).casefold()
    assert "local-test-key" not in log_text
    assert "authorization" not in log_text
    assert "traceback" not in log_text
    for port in (api_port, stub_port):
        with socket.socket() as connection:
            connection.settimeout(0.2)
            assert connection.connect_ex(("127.0.0.1", port)) != 0
    for log_path in (api_log_path, stub_log_path):
        log_path.unlink(missing_ok=True)
