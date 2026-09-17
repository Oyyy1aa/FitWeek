"""Three-Uvicorn Phase 6A acceptance using local Model and Calendar Stubs."""

import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tests.api.helpers import generation_payload, profile_payload

pytestmark = pytest.mark.phase_6a


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


def _wait_http(url: str) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=0.2).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.03)
    pytest.fail(f"HTTP service did not start: {url}")


def _payload(plan: dict[str, object], request_id: str) -> dict[str, object]:
    windows = []
    for session in plan["sessions"]:  # type: ignore[union-attr]
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


def test_real_model_calendar_and_fitweek_http_phase_6a(tmp_path: Path) -> None:
    model_port, calendar_port, api_port = _free_port(), _free_port(), _free_port()
    paths = [tmp_path / name for name in ("model.log", "calendar.log", "api.log")]
    environment = os.environ.copy()
    environment.update(
        {
            "PERSISTENCE_BACKEND": "memory",
            "REDIS_ENABLED": "false",
            "ORCHESTRATOR_ENABLED": "false",
            "MODEL_GATEWAY_ENABLED": "true",
            "MODEL_PRIMARY_PROVIDER": "http",
            "MODEL_PRIMARY_BASE_URL": f"http://127.0.0.1:{model_port}",
            "MODEL_PRIMARY_API_KEY": "local-test-key",
            "MODEL_PRIMARY_MODEL": "phase-6a-stub",
            "MODEL_BACKUP_PROVIDER": "template-fallback",
            "MODEL_REQUEST_TIMEOUT_SECONDS": "1",
            "CALENDAR_READ_ENABLED": "true",
            "CALENDAR_READ_PROVIDER": "http",
            "CALENDAR_READ_BASE_URL": f"http://127.0.0.1:{calendar_port}",
            "CALENDAR_READ_API_KEY": "local-test-key",
            "CALENDAR_READ_TIMEOUT_SECONDS": "1",
            "CONTEXT_DEBUG_API_ENABLED": "false",
        }
    )
    logs = [path.open("w", encoding="utf-8") for path in paths]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.stub_model_server.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(model_port),
                "--log-level",
                "warning",
            ],
            stdout=logs[0],
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ),
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.stub_calendar_server.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(calendar_port),
                "--log-level",
                "warning",
            ],
            stdout=logs[1],
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
            stdout=logs[2],
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ),
    ]
    try:
        _wait_http(f"http://127.0.0.1:{model_port}/openapi.json")
        _wait_http(f"http://127.0.0.1:{calendar_port}/openapi.json")
        _wait_http(f"http://127.0.0.1:{api_port}/health/live")
        with httpx.Client(
            base_url=f"http://127.0.0.1:{api_port}", timeout=10
        ) as client:
            assert (
                client.put("/api/v1/profiles/me", json=profile_payload()).status_code
                == 200
            )
            generated = client.post(
                "/api/v1/plans/generate", json=_future_generation_payload()
            ).json()["plan"]
            confirmed_response = client.post(
                f"/api/v1/plans/{generated['id']}/confirm",
                json={"expected_version": generated["version"]},
            )
            assert confirmed_response.status_code == 200
            plan = confirmed_response.json()
            before_plans = client.get("/api/v1/plans").json()
            payload = _payload(plan, "phase-6a-real-positive")
            created = client.post("/api/v1/schedule-drafts", json=payload)
            assert created.status_code == 201, created.text
            draft = created.json()
            assert draft["outcome"] == "COMPLETE"
            assert draft["source"] == "MODEL"
            assert draft["fallback_used"] is False
            busy = client.get(
                f"/api/v1/schedule-drafts/{draft['id']}/busy-snapshot"
            ).json()
            assert busy["mode"] == "PROVIDER"
            assert all("title" not in item for item in busy["intervals"])
            assert (
                client.get(f"/api/v1/schedule-drafts/{draft['id']}/trace").status_code
                == 200
            )
            accepted = client.post(
                f"/api/v1/schedule-drafts/{draft['id']}/accept",
                json={"expected_version": draft["version"]},
            )
            assert accepted.status_code == 200
            assert client.get("/api/v1/plans").json() == before_plans
            reused = client.post("/api/v1/schedule-drafts", json=payload)
            assert reused.status_code == 200 and reused.json()["id"] == draft["id"]
            conflict_payload = dict(payload)
            conflict_payload["timezone"] = "Etc/UTC"
            assert (
                client.post(
                    "/api/v1/schedule-drafts", json=conflict_payload
                ).status_code
                == 409
            )

            # Calendar failure degrades to MANUAL_ONLY without restarting FitWeek.
            processes[1].terminate()
            processes[1].wait(timeout=5)
            calendar_down = client.post(
                "/api/v1/schedule-drafts",
                json=_payload(plan, "phase-6a-real-calendar-down"),
            )
            assert calendar_down.status_code == 201, calendar_down.text
            calendar_down_draft = calendar_down.json()
            calendar_down_busy = client.get(
                f"/api/v1/schedule-drafts/{calendar_down_draft['id']}/busy-snapshot"
            ).json()
            assert calendar_down_busy["mode"] == "MANUAL_ONLY"
            assert calendar_down_draft["calendar_verification_status"] == "MANUAL_ONLY"

            # Model failure reuses the frozen Candidate Set through the
            # deterministic fallback.
            processes[0].terminate()
            processes[0].wait(timeout=5)
            model_down = client.post(
                "/api/v1/schedule-drafts",
                json=_payload(plan, "phase-6a-real-model-down"),
            )
            assert model_down.status_code == 201, model_down.text
            model_down_draft = model_down.json()
            assert model_down_draft["outcome"] == "COMPLETE"
            assert model_down_draft["source"] == "DETERMINISTIC_FALLBACK"
            assert model_down_draft["fallback_used"] is True

            partial_payload = _payload(plan, "phase-6a-real-partial")
            first_window = partial_payload["availability_windows"][0]
            first_start = datetime.fromisoformat(first_window["start"])
            partial_payload["availability_windows"] = [
                {
                    "start": first_start.isoformat(),
                    "end": (first_start + timedelta(minutes=30)).isoformat(),
                    "location": "HOME",
                }
            ]
            partial = client.post("/api/v1/schedule-drafts", json=partial_payload)
            assert partial.status_code == 201 and partial.json()["outcome"] == "PARTIAL"
            assert (
                client.post(
                    f"/api/v1/schedule-drafts/{partial.json()['id']}/accept",
                    json={"expected_version": partial.json()["version"]},
                ).status_code
                == 422
            )
            blocked = _payload(plan, "phase-6a-real-blocked")
            blocked["manual_busy_windows"] = [
                {"start": item["start"], "end": item["end"]}
                for item in blocked["availability_windows"]
            ]
            assert (
                client.post("/api/v1/schedule-drafts", json=blocked).status_code == 422
            )
            old = _payload(plan, "phase-6a-real-old-version")
            old["expected_plan_version"] = 1
            assert client.post("/api/v1/schedule-drafts", json=old).status_code == 409
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "local-test-key" not in combined
    assert "Authorization" not in combined
    assert "traceback" not in combined.casefold()
