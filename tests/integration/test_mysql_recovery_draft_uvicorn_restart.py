"""Independent-Uvicorn recovery for formal MySQL Recovery Drafts."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.persistence.database import Database
from tests.integration.test_mysql_recovery_draft_runtime import (
    _cleanup_recovery,
)
from tests.integration.test_mysql_session_design_draft_runtime import (
    _generation_payload,
)

_PROXY_NAMES = (
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)


def _free_port() -> int:
    with closing(socket.socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _port_is_free(port: int) -> bool:
    with closing(socket.socket()) as candidate:
        candidate.settimeout(0.2)
        return candidate.connect_ex(("127.0.0.1", port)) != 0


def _wait_for_http(
    base_url: str, process: subprocess.Popen[object], *, label: str
) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"owned {label} Uvicorn process exited during startup")
        try:
            if httpx.get(f"{base_url}/health/live", timeout=0.25).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.03)
    pytest.fail(f"owned {label} Uvicorn process did not become ready")


def _stop_owned_process(process: subprocess.Popen[object] | None, port: int) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)
    assert process.poll() is not None
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if _port_is_free(port):
            return
        time.sleep(0.03)
    pytest.fail("owned Recovery Uvicorn port was not released")


def _child_environment(mysql_test_url: str, *, email: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in _PROXY_NAMES:
        environment.pop(name, None)
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    environment.update(
        {
            "APP_ENV": "test",
            "PERSISTENCE_BACKEND": "mysql",
            "DATABASE_URL": mysql_test_url,
            "SINGLE_USER_EMAIL": email,
            "REDIS_ENABLED": "false",
            "ORCHESTRATOR_ENABLED": "false",
            "MODEL_GATEWAY_ENABLED": "false",
            "RECOVERY_AGENT_ENABLED": "false",
            "CONTEXT_DEBUG_API_ENABLED": "false",
        }
    )
    return environment


def _start_uvicorn(
    *,
    port: int,
    environment: dict[str, str],
    log: object,
) -> subprocess.Popen[object]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _profile_payload() -> dict[str, object]:
    return {
        "experience_level": "BEGINNER",
        "weekly_frequency": 2,
        "max_session_minutes": 45,
        "primary_goal": "GENERAL_FITNESS",
        "scope_confirmed": True,
    }


def _recovery_payload(token: str, plan: dict[str, Any]) -> dict[str, object]:
    return {
        "client_request_id": f"recovery-uvicorn-{token}",
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "request_type": "RESCHEDULE_REQUEST",
        "target_session_ids": [plan["sessions"][0]["id"]],
        "user_request": "Please reschedule this future training session.",
    }


async def _create_confirmed_plan(
    client: httpx.AsyncClient, token: str
) -> dict[str, Any]:
    profile = await client.put("/api/v1/profiles/me", json=_profile_payload())
    assert profile.status_code == 200, profile.text
    generated = await client.post(
        "/api/v1/plans/generate", json=_generation_payload(token)
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed = await client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


async def _read_artifacts(
    client: httpx.AsyncClient, draft_id: str
) -> dict[str, object]:
    values: dict[str, object] = {}
    for suffix in (
        "behavior-summary",
        "change-impact",
        "candidate-set",
        "memory-proposals",
        "trace",
    ):
        response = await client.get(f"/api/v1/recovery-drafts/{draft_id}/{suffix}")
        assert response.status_code == 200, response.text
        values[suffix] = response.json()
    return values


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_recovery_draft_survives_two_independent_uvicorn_processes(
    tmp_path: Path,
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    token = uuid4().hex
    port_a, port_b = _free_port(), _free_port()
    assert port_a != port_b
    url_a = f"http://127.0.0.1:{port_a}"
    url_b = f"http://127.0.0.1:{port_b}"
    email = f"recovery-uvicorn-{token}@fitweek.test"
    environment = _child_environment(mysql_test_url, email=email)
    process_a: subprocess.Popen[object] | None = None
    process_b: subprocess.Popen[object] | None = None
    user_id: UUID | None = None
    payload: dict[str, object] = {}
    draft_body: dict[str, object] = {}
    artifacts_a: dict[str, object] = {}
    log_a_path = tmp_path / "recovery-uvicorn-a.log"
    log_b_path = tmp_path / "recovery-uvicorn-b.log"
    try:
        with log_a_path.open("w", encoding="utf-8") as log_a:
            process_a = _start_uvicorn(port=port_a, environment=environment, log=log_a)
            _wait_for_http(url_a, process_a, label="A")
            async with httpx.AsyncClient(base_url=url_a, timeout=10) as client:
                user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
                plan = await _create_confirmed_plan(client, token)
                payload = _recovery_payload(token, plan)
                created = await client.post("/api/v1/recovery-drafts", json=payload)
                assert created.status_code == 201, created.text
                draft_body = created.json()
                assert draft_body["source"] == "DETERMINISTIC_FALLBACK"
                assert draft_body["fallback_used"] is True
                assert draft_body["status"] == "PENDING_REVIEW"
                created_at = datetime.fromisoformat(
                    str(draft_body["created_at"]).replace("Z", "+00:00")
                )
                assert created_at.utcoffset() == UTC.utcoffset(created_at)
                artifacts_a = await _read_artifacts(client, str(draft_body["id"]))
        first_pid = process_a.pid
        _stop_owned_process(process_a, port_a)
        process_a = None

        with log_b_path.open("w", encoding="utf-8") as log_b:
            process_b = _start_uvicorn(port=port_b, environment=environment, log=log_b)
            assert process_b.pid != first_pid
            _wait_for_http(url_b, process_b, label="B")
            async with httpx.AsyncClient(base_url=url_b, timeout=10) as client:
                replay = await client.post("/api/v1/recovery-drafts", json=payload)
                assert replay.status_code == 200, replay.text
                assert replay.json() == draft_body
                assert (
                    await _read_artifacts(client, str(draft_body["id"])) == artifacts_a
                )
                accepted = await client.post(
                    f"/api/v1/recovery-drafts/{draft_body['id']}/accept",
                    json={"expected_version": 1},
                )
                assert accepted.status_code == 200, accepted.text
                assert accepted.json()["status"] == "ACCEPTED"
                assert accepted.json()["version"] == 2
                stale = await client.post(
                    f"/api/v1/recovery-drafts/{draft_body['id']}/reject",
                    json={"expected_version": 1},
                )
                assert stale.status_code == 409, stale.text
                restored = await client.get(
                    f"/api/v1/recovery-drafts/{draft_body['id']}"
                )
                assert restored.status_code == 200, restored.text
                assert restored.json() == accepted.json()
                assert (
                    await _read_artifacts(client, str(draft_body["id"])) == artifacts_a
                )
    finally:
        _stop_owned_process(process_a, port_a)
        _stop_owned_process(process_b, port_b)
        if user_id is not None:
            await _cleanup_recovery(mysql_test_database, (user_id,))
