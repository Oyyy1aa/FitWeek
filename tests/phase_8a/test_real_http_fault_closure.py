"""Real loopback HTTP evidence for Phase 8A reliability behavior."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.api.helpers import profile_payload
from tests.support.http_server import shutdown_processes

pytestmark = pytest.mark.phase_8a


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


def _week(offset: int = 1) -> date:
    today = datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday())
    return monday + timedelta(weeks=offset)


def _generation_payload(offset: int = 1) -> dict[str, object]:
    week_start = _week(offset)
    return {
        "week_start": week_start.isoformat(),
        "availability_slots": [
            {
                "start": datetime.combine(
                    week_start + timedelta(days=day),
                    datetime.min.time(),
                    tzinfo=UTC,
                )
                .replace(hour=10)
                .isoformat(),
                "end": datetime.combine(
                    week_start + timedelta(days=day),
                    datetime.min.time(),
                    tzinfo=UTC,
                )
                .replace(hour=11)
                .isoformat(),
                "location_type": "HOME",
            }
            for day in (0, 2)
        ],
        "preferred_locations": ["HOME"],
        "preferred_session_types": [],
    }


def _confirmed_plan(client: httpx.Client, offset: int = 1) -> dict[str, Any]:
    assert client.put("/api/v1/profiles/me", json=profile_payload()).status_code == 200
    generated = client.post("/api/v1/plans/generate", json=_generation_payload(offset))
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed = client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _schedule_payload(plan: dict[str, Any], request_id: str) -> dict[str, object]:
    windows = []
    for session in plan["sessions"]:
        start = datetime.fromisoformat(
            session["scheduled_start"].replace("Z", "+00:00")
        ) + timedelta(hours=2)
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


@contextmanager
def _running_stack(
    tmp_path: Path,
    *,
    write: bool = False,
    failure_threshold: int = 5,
    orchestrator: bool = False,
) -> Iterator[tuple[httpx.Client, httpx.Client, tuple[Path, Path]]]:
    stub_port, api_port = _free_port(), _free_port()
    stub_log, api_log = tmp_path / "calendar.log", tmp_path / "fitweek.log"
    environment = os.environ.copy()
    environment.update(
        {
            "PERSISTENCE_BACKEND": "memory",
            "REDIS_ENABLED": "false",
            "ORCHESTRATOR_ENABLED": "true" if orchestrator else "false",
            "MODEL_GATEWAY_ENABLED": "false",
            "CALENDAR_READ_ENABLED": "true",
            "CALENDAR_READ_PROVIDER": "http",
            "CALENDAR_READ_BASE_URL": f"http://127.0.0.1:{stub_port}",
            "CALENDAR_READ_API_KEY": "local-calendar-read-test-key",
            "CALENDAR_READ_TIMEOUT_SECONDS": "0.15",
            "CALENDAR_READ_MAX_ATTEMPTS": "2",
            "CALENDAR_READ_BULKHEAD_LIMIT": "2",
            "TOOL_CIRCUIT_FAILURE_THRESHOLD": str(failure_threshold),
            "TOOL_CIRCUIT_FAILURE_WINDOW_SECONDS": "60",
            "TOOL_CIRCUIT_OPEN_DURATION_SECONDS": "1",
            "TOOL_RETRY_BUDGET_MAXIMUM_ATTEMPTS": "20",
            "SCHEDULE_BUSY_SNAPSHOT_MAX_AGE_SECONDS": "1",
            "CONTEXT_DEBUG_API_ENABLED": "false",
        }
    )
    if write:
        environment.update(
            {
                "CALENDAR_WRITE_ENABLED": "true",
                "CALENDAR_WRITE_PROVIDER": "http",
                "CALENDAR_WRITE_BASE_URL": f"http://127.0.0.1:{stub_port}",
                "CALENDAR_WRITE_API_KEY": "local-calendar-write-test-key",
                "CALENDAR_WRITE_TIMEOUT_SECONDS": "0.3",
                "CALENDAR_WRITE_MAX_ATTEMPTS": "3",
            }
        )
    opened = [
        stub_log.open("w", encoding="utf-8"),
        api_log.open("w", encoding="utf-8"),
    ]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.stub_calendar_server.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(stub_port),
                "--log-level",
                "warning",
            ],
            stdout=opened[0],
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ),
        subprocess.Popen(
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
            stdout=opened[1],
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ),
    ]
    try:
        _wait_http(f"http://127.0.0.1:{stub_port}/openapi.json")
        _wait_http(f"http://127.0.0.1:{api_port}/health/live")
        with (
            httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=10) as api,
            httpx.Client(base_url=f"http://127.0.0.1:{stub_port}", timeout=3) as stub,
        ):
            assert stub.post("/admin/reset").status_code == 200
            yield api, stub, (stub_log, api_log)
    finally:
        shutdown_processes(processes, ports=(stub_port, api_port))
        for value in opened:
            value.close()


def _assert_clean_logs(paths: tuple[Path, Path]) -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "local-calendar" not in combined
    assert "Authorization" not in combined
    assert "traceback" not in combined.casefold()


def test_real_http_calendar_read_circuit_and_half_open(tmp_path: Path) -> None:
    with _running_stack(tmp_path) as (api, stub, logs):
        plan = _confirmed_plan(api)
        assert (
            stub.post(
                "/admin/read-control", json={"scenario": "server-error"}
            ).status_code
            == 200
        )
        before = stub.get("/admin/read-stats").json()["request_count"]
        for index in range(5):
            failed = api.post(
                "/api/v1/schedule-drafts",
                json=_schedule_payload(plan, f"read-open-{index}"),
            )
            assert failed.status_code == 201, failed.text
            assert failed.json()["calendar_verification_status"] == "MANUAL_ONLY"
        after_failures = stub.get("/admin/read-stats").json()["request_count"]
        assert after_failures - before == 10
        open_state = api.get("/api/v1/tool-gateway/circuits").json()["open_circuits"]
        assert any("CALENDAR_FREE_BUSY" in item for item in open_state)
        rejected = api.post(
            "/api/v1/schedule-drafts",
            json=_schedule_payload(plan, "read-circuit-rejected"),
        )
        assert rejected.status_code == 201
        assert rejected.json()["calendar_verification_status"] == "MANUAL_ONLY"
        assert stub.get("/admin/read-stats").json()["request_count"] == after_failures

        assert (
            stub.post("/admin/read-control", json={"scenario": "success"}).status_code
            == 200
        )
        time.sleep(1.05)
        recovered = api.post(
            "/api/v1/schedule-drafts",
            json=_schedule_payload(plan, "read-half-open-success"),
        )
        assert recovered.status_code == 201, recovered.text
        assert recovered.json()["calendar_verification_status"] == "VERIFIED"
        assert api.get("/api/v1/tool-gateway/circuits").json()["open_circuits"] == []
        summaries = [
            item
            for item in api.get("/api/v1/tool-gateway/invocations").json()
            if item["tool_id"] == "CALENDAR_FREE_BUSY"
        ]
        assert summaries[-1]["circuit_before"] == "HALF_OPEN"
        assert summaries[-1]["circuit_after"] == "CLOSED"
        assert [item["attempt_count"] for item in summaries[:6]] == [
            2,
            2,
            2,
            2,
            2,
            0,
        ]

        assert (
            stub.post(
                "/admin/read-control", json={"scenario": "server-error"}
            ).status_code
            == 200
        )
        for index in range(5):
            api.post(
                "/api/v1/schedule-drafts",
                json=_schedule_payload(plan, f"read-reopen-{index}"),
            )
        time.sleep(1.05)
        probe = api.post(
            "/api/v1/schedule-drafts",
            json=_schedule_payload(plan, "read-half-open-failure"),
        )
        assert probe.status_code == 201
        probe_summary = [
            item
            for item in api.get("/api/v1/tool-gateway/invocations").json()
            if item["tool_id"] == "CALENDAR_FREE_BUSY"
        ][-1]
        assert probe_summary["circuit_before"] == "HALF_OPEN"
        assert probe_summary["circuit_after"] == "OPEN"
        metrics = api.get("/api/v1/tool-gateway/metrics").json()["counts"]
        assert metrics["tool_circuit_open_total"] >= 2
        assert metrics["tool_circuit_rejections_total"] >= 1
    _assert_clean_logs(logs)


def test_real_http_deadline_and_bulkhead_release(tmp_path: Path) -> None:
    with _running_stack(tmp_path) as (api, stub, logs):
        plan = _confirmed_plan(api)
        assert (
            stub.post(
                "/admin/read-control",
                json={"scenario": "success", "delay_seconds": 0.4},
            ).status_code
            == 200
        )
        started = time.monotonic()
        deadline = api.post(
            "/api/v1/schedule-drafts",
            json=_schedule_payload(plan, "deadline-http"),
        )
        elapsed = time.monotonic() - started
        assert deadline.status_code == 201, deadline.text
        assert deadline.json()["calendar_verification_status"] == "MANUAL_ONLY"
        assert elapsed < 1.0
        summary = [
            item
            for item in api.get("/api/v1/tool-gateway/invocations").json()
            if item["tool_id"] == "CALENDAR_FREE_BUSY"
        ][-1]
        assert summary["error_category"] == "DEADLINE_EXCEEDED"
        assert summary["attempt_count"] <= 2
        metrics = api.get("/api/v1/tool-gateway/metrics").json()["counts"]
        assert metrics["tool_deadline_exceeded_total"] >= 1
        time.sleep(0.5)
        assert stub.get("/admin/read-stats").json()["active"] == 0

        assert stub.post("/admin/reset").status_code == 200
        assert (
            stub.post(
                "/admin/read-control",
                json={"scenario": "success", "delay_seconds": 0.05},
            ).status_code
            == 200
        )

        def create(index: int) -> int:
            with httpx.Client(base_url=str(api.base_url), timeout=10) as other:
                return other.post(
                    "/api/v1/schedule-drafts",
                    json=_schedule_payload(plan, f"bulkhead-http-{index}"),
                ).status_code

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(create, index) for index in range(5)]
            time.sleep(0.02)
            root = plan["root_plan_id"] or plan["id"]
            exported = api.post(
                f"/api/v1/plans/{root}/revisions/1/ics-export",
                json={
                    "client_request_id": "bulkhead-independent-ics",
                    "expected_plan_version": plan["version"],
                },
            )
            statuses = [future.result(timeout=10) for future in futures]
        assert exported.status_code == 201, exported.text
        assert statuses == [201] * 5
        stats = stub.get("/admin/read-stats").json()
        assert stats["max_concurrency"] <= 2
        assert stats["active"] == 0
    _assert_clean_logs(logs)


def _calendar_operation(
    api: httpx.Client, plan: dict[str, Any], request_id: str
) -> dict[str, Any]:
    root = plan["root_plan_id"] or plan["id"]
    created = api.post(
        f"/api/v1/plans/{root}/revisions/1/calendar-operation-drafts",
        json={
            "client_request_id": request_id,
            "expected_plan_version": plan["version"],
            "provider": "http",
            "calendar_id": "primary",
        },
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    approved = api.post(
        f"/api/v1/calendar-operation-drafts/{draft['id']}/approve",
        json={"expected_version": draft["version"]},
    )
    assert approved.status_code == 200, approved.text
    return approved.json()


def _enqueue_calendar_run(
    api: httpx.Client, draft: dict[str, Any], request_id: str
) -> dict[str, Any]:
    response = api.post(
        "/api/v1/calendar-operation-runs",
        json={"client_request_id": request_id, "draft_id": draft["id"]},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _wait_calendar_run(api: httpx.Client, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = api.get(f"/api/v1/calendar-operation-runs/{run_id}")
        assert response.status_code == 200, response.text
        if response.json()["status"] in {"COMPLETED", "FAILED_PERMANENT"}:
            return response.json()
        time.sleep(0.05)
    pytest.fail("Calendar Run did not reach terminal state.")


def _wait_for_open_circuit_recovery(
    api: httpx.Client,
    stub: httpx.Client,
    run_id: str,
    draft_id: str,
    expected_write_count: int = 6,
) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    observed_open = False
    while time.monotonic() < deadline:
        run = api.get(f"/api/v1/calendar-operation-runs/{run_id}")
        draft = api.get(f"/api/v1/calendar-operation-drafts/{draft_id}")
        assert run.status_code == 200 and draft.status_code == 200
        events = stub.get("/admin/events").json()
        circuits = api.get("/api/v1/tool-gateway/circuits").json()["open_circuits"]
        if (
            draft.json()["status"] == "PARTIALLY_SUCCEEDED"
            and events["event_count"] == 0
            and events["write_request_count"] == expected_write_count
            and any("CALENDAR_COMMIT" in item for item in circuits)
        ):
            observed_open = True
        if run.json()["status"] in {"COMPLETED", "FAILED_PERMANENT"}:
            assert observed_open
            return run.json()
        time.sleep(0.05)
    pytest.fail("Calendar Run did not show open-circuit recovery facts.")


def test_real_http_calendar_write_circuit_and_response_lost(tmp_path: Path) -> None:
    with _running_stack(
        tmp_path, write=True, failure_threshold=2, orchestrator=True
    ) as (
        api,
        stub,
        logs,
    ):
        plan = _confirmed_plan(api)
        draft = _calendar_operation(api, plan, "write-circuit")
        before = stub.get("/admin/events").json()
        assert (
            api.post(
                f"/api/v1/calendar-operation-drafts/{draft['id']}/execute"
            ).status_code
            == 409
        )
        assert (
            api.post(
                f"/api/v1/calendar-operation-drafts/{draft['id']}/retry"
            ).status_code
            == 409
        )
        assert stub.get("/admin/events").json() == before
        assert (
            stub.post(
                "/admin/fail-next",
                json={"status_code": 500, "after_commit": False, "count": 6},
            ).status_code
            == 200
        )
        failed_run = _enqueue_calendar_run(api, draft, "write-circuit-run")
        assert (
            _wait_for_open_circuit_recovery(api, stub, failed_run["id"], draft["id"])[
                "status"
            ]
            == "COMPLETED"
        )
        failed = api.get(f"/api/v1/calendar-operation-drafts/{draft['id']}")
        assert failed.status_code == 200, failed.text
        assert failed.json()["status"] == "SUCCEEDED"
        assert all(item["status"] == "SUCCEEDED" for item in failed.json()["items"])
        blocked = api.post(f"/api/v1/calendar-operation-drafts/{draft['id']}/retry")
        assert blocked.status_code == 409
        events = stub.get("/admin/events").json()
        assert events["event_count"] == len(plan["sessions"])
        root = plan["root_plan_id"] or plan["id"]
        bindings = api.get(
            "/api/v1/calendar-bindings",
            params={
                "provider": "http",
                "calendar_id": "primary",
                "root_plan_id": root,
            },
        ).json()
        assert len(bindings) == len(plan["sessions"])

        plan_two = _confirmed_plan(api, offset=2)
        assert stub.post("/admin/reset").status_code == 200
        response_lost = _calendar_operation(api, plan_two, "response-lost")
        response_before = stub.get("/admin/events").json()
        assert (
            api.post(
                f"/api/v1/calendar-operation-drafts/{response_lost['id']}/execute"
            ).status_code
            == 409
        )
        assert (
            api.post(
                f"/api/v1/calendar-operation-drafts/{response_lost['id']}/retry"
            ).status_code
            == 409
        )
        assert stub.get("/admin/events").json() == response_before
        assert (
            stub.post(
                "/admin/fail-next",
                json={"status_code": 500, "after_commit": True, "count": 1},
            ).status_code
            == 200
        )
        response_run = _enqueue_calendar_run(api, response_lost, "response-lost-run")
        assert _wait_calendar_run(api, response_run["id"])["status"] == "COMPLETED"
        executed = api.get(f"/api/v1/calendar-operation-drafts/{response_lost['id']}")
        assert executed.status_code == 200, executed.text
        assert executed.json()["status"] == "SUCCEEDED"
        response_stats = stub.get("/admin/events").json()
        assert response_stats["event_count"] == len(plan_two["sessions"])
        assert response_stats["write_request_count"] == len(plan_two["sessions"]) + 1
        assert response_stats["duplicate_reuse_count"] == 1
        summaries = [
            item
            for item in api.get("/api/v1/tool-gateway/invocations").json()
            if item["tool_id"] == "CALENDAR_COMMIT"
        ]
        assert any(item["attempt_count"] == 2 for item in summaries)
        attempts = api.get(
            f"/api/v1/calendar-operation-drafts/{response_lost['id']}/attempts"
        ).json()
        assert len(attempts) == len(plan_two["sessions"]) + 1
    _assert_clean_logs(logs)


def _accepted_schedule(
    api: httpx.Client,
    plan: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    created = api.post(
        "/api/v1/schedule-drafts",
        json=_schedule_payload(plan, request_id),
    )
    assert created.status_code == 201, created.text
    accepted = api.post(
        f"/api/v1/schedule-drafts/{created.json()['id']}/accept",
        json={"expected_version": created.json()["version"]},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


def _schedule_apply_payload(
    plan: dict[str, Any], draft: dict[str, Any], request_id: str
) -> dict[str, object]:
    return {
        "client_request_id": request_id,
        "expected_draft_version": draft["version"],
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
    }


def test_real_http_stale_busy_rejects_failure_and_new_conflict(
    tmp_path: Path,
) -> None:
    with _running_stack(tmp_path) as (api, stub, logs):
        plan = _confirmed_plan(api)
        draft = _accepted_schedule(api, plan, "stale-busy-draft")
        root = plan["root_plan_id"] or plan["id"]
        before_revisions = api.get(f"/api/v1/plans/{root}/revisions").json()
        time.sleep(2.05)
        assert (
            stub.post(
                "/admin/read-control", json={"scenario": "server-error"}
            ).status_code
            == 200
        )
        payload = _schedule_apply_payload(plan, draft, "stale-busy-apply")
        failed = api.post(
            f"/api/v1/schedule-drafts/{draft['id']}/apply-preview",
            json=payload,
        )
        assert failed.status_code == 409, failed.text
        assert failed.json()["error"]["code"] == (
            "SCHEDULE_CALENDAR_REVALIDATION_FAILED"
        )
        assert api.get(f"/api/v1/plans/{root}/revisions").json() == before_revisions
        assert (
            api.get(f"/api/v1/schedule-drafts/{draft['id']}").json()["status"]
            == "ACCEPTED"
        )

        assignment = draft["assignments"][0]
        assert (
            stub.post(
                "/admin/read-control",
                json={
                    "scenario": "success",
                    "busy": [
                        {
                            "start": assignment["scheduled_start"],
                            "end": assignment["scheduled_end"],
                            "transparency": "OPAQUE",
                        }
                    ],
                },
            ).status_code
            == 200
        )
        conflict = api.post(
            f"/api/v1/schedule-drafts/{draft['id']}/apply-preview",
            json=payload,
        )
        assert conflict.status_code == 422, conflict.text
        assert conflict.json()["error"]["code"] == "SCHEDULE_NEW_CALENDAR_CONFLICT"
        assert api.get(f"/api/v1/plans/{root}/revisions").json() == before_revisions
        compatibility = api.get("/api/v1/tool-gateway/compatibility-traces").json()
        stale = [
            item
            for item in compatibility
            if item["component"] == "CALENDAR_STALE_REVALIDATION"
        ]
        assert {item["error_code"] for item in stale} == {
            "SCHEDULE_CALENDAR_REVALIDATION_FAILED",
            "SCHEDULE_NEW_CALENDAR_CONFLICT",
        }
        counts = api.get("/api/v1/tool-gateway/metrics").json()["counts"]
        assert counts["stale_data_rejected_total"] == 2
    _assert_clean_logs(logs)


def test_real_http_concurrent_plan_cas_creates_one_revision(tmp_path: Path) -> None:
    with _running_stack(tmp_path) as (api, stub, logs):
        plan = _confirmed_plan(api)
        first = _accepted_schedule(api, plan, "cas-draft-one")
        second = _accepted_schedule(api, plan, "cas-draft-two")
        before_attempts = (
            api.get("/api/v1/tool-gateway/metrics")
            .json()["counts"]
            .get("tool_attempts_total", 0)
        )

        def apply(draft: dict[str, Any], request_id: str) -> int:
            with httpx.Client(base_url=str(api.base_url), timeout=10) as other:
                return other.post(
                    f"/api/v1/schedule-drafts/{draft['id']}/apply",
                    json=_schedule_apply_payload(plan, draft, request_id),
                ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(
                (
                    pool.submit(apply, first, "cas-apply-one"),
                    pool.submit(apply, second, "cas-apply-two"),
                ),
                key=id,
            )
            statuses = sorted(item.result(timeout=10) for item in outcomes)
        assert statuses == [201, 409]
        root = plan["root_plan_id"] or plan["id"]
        revisions = api.get(f"/api/v1/plans/{root}/revisions").json()
        assert [item["plan"]["revision"] for item in revisions] == [1, 2]
        assert sum(item["is_current_revision"] for item in revisions) == 1
        after_attempts = (
            api.get("/api/v1/tool-gateway/metrics")
            .json()["counts"]
            .get("tool_attempts_total", 0)
        )
        assert after_attempts == before_attempts
        assert stub.get("/admin/read-stats").json()["active"] == 0
    _assert_clean_logs(logs)
