"""Two-process recovery for durable MySQL Profile Agent drafts."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, select

from app.persistence.database import Database
from app.persistence.mysql.models import (
    AuditEventModel,
    ContextSnapshotModel,
    FitnessProfileModel,
    IdempotencyRecordModel,
    ProfileDraftModel,
    UserAccountModel,
    UserConstraintModel,
)


def _free_port() -> int:
    with closing(socket.socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _port_is_free(port: int) -> bool:
    with closing(socket.socket()) as candidate:
        candidate.settimeout(0.2)
        return candidate.connect_ex(("127.0.0.1", port)) != 0


def _wait_for_url(url: str, process: subprocess.Popen[object], *, label: str) -> None:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"owned {label} process exited before becoming available")
        try:
            if httpx.get(url, timeout=0.25).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.03)
    pytest.fail(f"owned {label} process did not become available")


def _stop_owned_process(process: subprocess.Popen[object], port: int) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=6)
    assert process.poll() is not None
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if _port_is_free(port):
            return
        time.sleep(0.03)
    pytest.fail("owned process port was not released")


def _start_uvicorn(
    target: str,
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
            target,
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


def _parse_payload(token: str) -> dict[str, object]:
    return {
        "client_request_id": f"profile-draft-uvicorn-parse-{token}",
        "user_message": (
            "I can train at home three times per week for 30 minutes "
            "using resistance bands."
        ),
        "current_week": "2026-07-20",
    }


def _decision(token: str) -> dict[str, object]:
    return {
        "client_request_id": f"profile-draft-uvicorn-apply-{token}",
        "expected_draft_version": 1,
        "expected_profile_version": None,
        "accept_weekly_frequency": True,
        "accept_max_session_minutes": True,
        "selected_primary_goal": "GENERAL_FITNESS",
        "accepted_equipment": ["resistance_band"],
        "accepted_locations": ["HOME"],
        "accepted_hard_constraint_indexes": [0],
        "accepted_temporary_constraint_indexes": [0],
        "temporary_constraint_expirations": {
            "0": (datetime.now(UTC) + timedelta(days=7)).isoformat()
        },
        "confirmed_experience_level": "BEGINNER",
        "confirm_scope": True,
    }


async def _cleanup(database: Database, user_id: UUID | None) -> None:
    if user_id is None:
        return
    user_id_text = str(user_id)
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(ProfileDraftModel).where(
                    ProfileDraftModel.user_id == user_id_text
                )
            )
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id == user_id_text)
            )
            await session.execute(
                delete(ContextSnapshotModel).where(
                    ContextSnapshotModel.user_id == user_id_text
                )
            )
            await session.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.user_id == user_id_text
                )
            )
            profile_ids = (
                await session.scalars(
                    select(FitnessProfileModel.id).where(
                        FitnessProfileModel.user_id == user_id_text
                    )
                )
            ).all()
            if profile_ids:
                await session.execute(
                    delete(UserConstraintModel).where(
                        UserConstraintModel.profile_id.in_(profile_ids)
                    )
                )
            await session.execute(
                delete(FitnessProfileModel).where(
                    FitnessProfileModel.user_id == user_id_text
                )
            )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id == user_id_text)
            )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("restart_iteration", range(3))
async def test_mysql_profile_draft_survives_two_uvicorn_processes(
    tmp_path: Path,
    mysql_test_database: Database,
    mysql_test_url: str,
    restart_iteration: int,
) -> None:
    token = f"{restart_iteration}-{uuid4().hex}"
    api_port, stub_port = _free_port(), _free_port()
    api_url = f"http://127.0.0.1:{api_port}"
    stub_url = f"http://127.0.0.1:{stub_port}"
    local_test_key = f"profile-draft-test-key-{uuid4().hex}"
    email = f"profile-draft-uvicorn-{token}@fitweek.test"
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "PERSISTENCE_BACKEND": "mysql",
            "DATABASE_URL": mysql_test_url,
            "SINGLE_USER_EMAIL": email,
            "REDIS_ENABLED": "false",
            "MODEL_GATEWAY_ENABLED": "true",
            "MODEL_PRIMARY_PROVIDER": "http",
            "MODEL_PRIMARY_BASE_URL": f"{stub_url}/profile-apply",
            "MODEL_PRIMARY_API_KEY": local_test_key,
            "MODEL_PRIMARY_MODEL": "controlled-profile-draft",
            "MODEL_BACKUP_PROVIDER": "template-fallback",
            "ORCHESTRATOR_ENABLED": "false",
            "CONTEXT_DEBUG_API_ENABLED": "false",
        }
    )
    stub: subprocess.Popen[object] | None = None
    process_a: subprocess.Popen[object] | None = None
    process_b: subprocess.Popen[object] | None = None
    user_id: UUID | None = None
    log_paths = (
        tmp_path / f"profile-draft-a-{restart_iteration}.log",
        tmp_path / f"profile-draft-b-{restart_iteration}.log",
        tmp_path / f"profile-draft-stub-{restart_iteration}.log",
    )
    try:
        with log_paths[2].open("w", encoding="utf-8") as stub_log:
            stub = _start_uvicorn(
                "tests.stub_model_server.app:app",
                port=stub_port,
                environment=os.environ.copy(),
                log=stub_log,
            )
            _wait_for_url(
                f"{stub_url}/openapi.json", stub, label="Profile Draft model stub"
            )

            with log_paths[0].open("w", encoding="utf-8") as api_a_log:
                process_a = _start_uvicorn(
                    "app.main:app",
                    port=api_port,
                    environment=environment,
                    log=api_a_log,
                )
                _wait_for_url(f"{api_url}/health/live", process_a, label="API A")
                async with httpx.AsyncClient(base_url=api_url, timeout=6) as client:
                    user = await client.get("/api/v1/users/me")
                    assert user.status_code == 200
                    user_id = UUID(user.json()["id"])
                    created = await client.post(
                        "/api/v1/profile-agent/parse", json=_parse_payload(token)
                    )
                    assert created.status_code == 201, created.text
                    draft_id = created.json()["id"]
                    snapshot_id = created.json()["context_snapshot_reference_id"]
                    fingerprint = created.json()["context_fingerprint"]
                    detail_a = await client.get(
                        f"/api/v1/profile-agent/drafts/{draft_id}"
                    )
                    assert detail_a.status_code == 200
                    assert (
                        detail_a.json()["context_snapshot_reference_id"] == snapshot_id
                    )
                    assert detail_a.json()["context_fingerprint"] == fingerprint

            _stop_owned_process(process_a, api_port)
            process_a = None

            with log_paths[1].open("w", encoding="utf-8") as api_b_log:
                process_b = _start_uvicorn(
                    "app.main:app",
                    port=api_port,
                    environment=environment,
                    log=api_b_log,
                )
                _wait_for_url(f"{api_url}/health/live", process_b, label="API B")
                async with httpx.AsyncClient(base_url=api_url, timeout=6) as client:
                    detail_b = await client.get(
                        f"/api/v1/profile-agent/drafts/{draft_id}"
                    )
                    assert detail_b.status_code == 200
                    assert detail_b.json()["id"] == draft_id
                    assert (
                        detail_b.json()["context_snapshot_reference_id"] == snapshot_id
                    )
                    assert detail_b.json()["context_fingerprint"] == fingerprint
                    decision = _decision(token)
                    preview = await client.post(
                        f"/api/v1/profile-agent/drafts/{draft_id}/apply-preview",
                        json=decision,
                    )
                    assert preview.status_code == 200, preview.text
                    applied = await client.post(
                        f"/api/v1/profile-agent/drafts/{draft_id}/apply",
                        json=decision,
                    )
                    assert applied.status_code == 200, applied.text
                    profile = await client.get("/api/v1/profiles/me")
                    assert profile.status_code == 200
                    assert profile.json()["id"] == applied.json()["profile_id"]
                    final_draft = await client.get(
                        f"/api/v1/profile-agent/drafts/{draft_id}"
                    )
                    assert final_draft.status_code == 200
                    assert final_draft.json()["status"] == "APPLIED"
                    assert (
                        final_draft.json()["context_snapshot_reference_id"]
                        == snapshot_id
                    )
                    model_count = await client.get(
                        f"{stub_url}/admin/count/profile-apply"
                    )
                    assert model_count.json() == {"count": 1}
    finally:
        if process_b is not None:
            _stop_owned_process(process_b, api_port)
        if process_a is not None:
            _stop_owned_process(process_a, api_port)
        if stub is not None:
            _stop_owned_process(stub, stub_port)
        await _cleanup(mysql_test_database, user_id)

    logs = "\n".join(
        path.read_text(encoding="utf-8") for path in log_paths if path.exists()
    )
    database_password = urlsplit(mysql_test_url).password
    assert local_test_key not in logs
    assert database_password is None or database_password not in logs
    assert "Traceback" not in logs
    assert _port_is_free(api_port)
    assert _port_is_free(stub_port)
