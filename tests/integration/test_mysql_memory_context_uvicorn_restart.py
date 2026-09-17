"""OS-process restart recovery for durable MySQL Memory and Context state."""

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
from pydantic import SecretStr
from sqlalchemy import delete, select

from app.config import PersistenceBackend, Settings
from app.infrastructure.redis_client import RedisManager
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AuditEventModel,
    ContextSnapshotModel,
    FitnessProfileModel,
    IdempotencyRecordModel,
    MemoryCandidateModel,
    MemoryEvidenceModel,
    MemoryItemModel,
    SessionExerciseModel,
    UserAccountModel,
    UserConstraintModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)
from tests.api.helpers import generation_payload, profile_payload


def _free_port() -> int:
    with closing(socket.socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _port_is_free(port: int) -> bool:
    with closing(socket.socket()) as candidate:
        candidate.settimeout(0.2)
        return candidate.connect_ex(("127.0.0.1", port)) != 0


def _wait_for_live(base_url: str, process: subprocess.Popen[object]) -> None:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("owned Uvicorn process exited before becoming live")
        try:
            if httpx.get(f"{base_url}/health/live", timeout=0.25).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.03)
    pytest.fail("owned Uvicorn process did not become live")


def _stop_owned_process(process: subprocess.Popen[object], port: int) -> None:
    """Stop only the process created by this test and wait for its port."""

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
    pytest.fail("owned Uvicorn port was not released after process termination")


def _start_api(
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


async def _cleanup_user(database: Database, user_id: UUID | None) -> None:
    if user_id is None:
        return
    user_id_text = str(user_id)
    async with database.session_factory() as session:
        async with session.begin():
            plan_ids = (
                await session.scalars(
                    select(WeeklyPlanModel.id).where(
                        WeeklyPlanModel.user_id == user_id_text
                    )
                )
            ).all()
            if plan_ids:
                session_ids = (
                    await session.scalars(
                        select(WorkoutSessionModel.id).where(
                            WorkoutSessionModel.plan_id.in_(plan_ids)
                        )
                    )
                ).all()
                if session_ids:
                    await session.execute(
                        delete(SessionExerciseModel).where(
                            SessionExerciseModel.session_id.in_(session_ids)
                        )
                    )
                await session.execute(
                    delete(WorkoutSessionModel).where(
                        WorkoutSessionModel.plan_id.in_(plan_ids)
                    )
                )
            await session.execute(
                delete(WeeklyPlanModel).where(WeeklyPlanModel.user_id == user_id_text)
            )
            memory_ids = (
                await session.scalars(
                    select(MemoryItemModel.id).where(
                        MemoryItemModel.user_id == user_id_text
                    )
                )
            ).all()
            if memory_ids:
                await session.execute(
                    delete(MemoryEvidenceModel).where(
                        MemoryEvidenceModel.memory_id.in_(memory_ids)
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
                delete(MemoryCandidateModel).where(
                    MemoryCandidateModel.user_id == user_id_text
                )
            )
            await session.execute(
                delete(MemoryItemModel).where(MemoryItemModel.user_id == user_id_text)
            )
            await session.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.user_id == user_id_text
                )
            )
            profile_ids = select(FitnessProfileModel.id).where(
                FitnessProfileModel.user_id == user_id_text
            )
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
@pytest.mark.parametrize("redis_mode", ["normal", "degraded"])
async def test_mysql_memory_context_survive_two_independent_uvicorn_processes(
    tmp_path: Path,
    mysql_test_database: Database,
    mysql_test_url: str,
    test_redis_url: str,
    redis_mode: str,
) -> None:
    """A fresh Uvicorn process restores MySQL facts with or without Redis."""

    run_id = uuid4().hex
    email = f"uvicorn-memory-restart-{run_id}@fitweek.test"
    prefix = f"fitweek:test:{run_id}"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update(
        {
            "PERSISTENCE_BACKEND": "mysql",
            "DATABASE_URL": mysql_test_url,
            "SINGLE_USER_EMAIL": email,
            "REDIS_ENABLED": "true",
            "REDIS_KEY_PREFIX": prefix,
            "REDIS_CONNECT_TIMEOUT_SECONDS": "0.1",
            "REDIS_SOCKET_TIMEOUT_SECONDS": "0.1",
            "ORCHESTRATOR_ENABLED": "false",
            "CONTEXT_DEBUG_API_ENABLED": "false",
        }
    )
    if redis_mode == "normal":
        environment["REDIS_URL"] = test_redis_url
    else:
        environment["REDIS_URL"] = "redis://127.0.0.1:1/15"

    process_a: subprocess.Popen[object] | None = None
    process_b: subprocess.Popen[object] | None = None
    user_id: UUID | None = None
    cache_manager: RedisManager | None = None
    cache_key: str | None = None
    process_a_log = tmp_path / f"memory-context-process-a-{redis_mode}.log"
    process_b_log = tmp_path / f"memory-context-process-b-{redis_mode}.log"
    try:
        with process_a_log.open("w", encoding="utf-8") as log_a:
            process_a = _start_api(port=port, environment=environment, log=log_a)
            _wait_for_live(base_url, process_a)
            async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
                ready_a = await client.get("/health/ready")
                assert ready_a.status_code == 200
                if redis_mode == "normal":
                    assert ready_a.json()["checks"]["redis"] == "ok"
                else:
                    assert ready_a.json()["status"] == "degraded"
                    assert ready_a.json()["checks"]["redis"] == "unavailable"
                user = await client.get("/api/v1/users/me")
                assert user.status_code == 200
                user_id = UUID(user.json()["id"])
                assert (
                    await client.put("/api/v1/profiles/me", json=profile_payload())
                ).status_code == 200
                candidate = await client.post(
                    "/api/v1/memory-candidates",
                    json={
                        "client_request_id": f"uvicorn-candidate-create-{run_id}",
                        "memory_type": "PREFERRED_LOCATION",
                        "key": "preferred_location",
                        "value": "HOME",
                        "source": "PROFILE_AGENT_CANDIDATE",
                        "source_reference": "uvicorn-process-restart",
                        "evidence_summary": "Structured process restart evidence.",
                        "confidence": None,
                        "expires_at": (
                            datetime.now(UTC) + timedelta(days=1)
                        ).isoformat(),
                    },
                )
                assert candidate.status_code == 201
                candidate_id = candidate.json()["id"]
                accepted = await client.post(
                    f"/api/v1/memory-candidates/{candidate_id}/accept",
                    json={
                        "client_request_id": f"uvicorn-candidate-accept-{run_id}",
                        "expected_candidate_version": 1,
                        "confirmed_value": "HOME",
                        "valid_until": None,
                    },
                )
                assert accepted.status_code == 200
                memory = accepted.json()["memory"]
                assert memory is not None
                memory_id = memory["id"]
                evidence_ids = [item["id"] for item in memory["evidence"]]
                assert evidence_ids
                generated = await client.post(
                    "/api/v1/plans/generate",
                    json={
                        **generation_payload(),
                        "client_request_id": f"uvicorn-context-plan-{run_id}",
                    },
                )
                assert generated.status_code == 201
                plan_id = generated.json()["plan"]["id"]
                generation = generated.json()["generation"]
                snapshot_id = generation["context_snapshot_reference_id"]
                fingerprint = generation["context_fingerprint"]
                snapshot = await client.get(f"/api/v1/context-snapshots/{snapshot_id}")
                assert snapshot.status_code == 200
                assert snapshot.json()["context_fingerprint"] == fingerprint
                assert memory_id in {
                    item["id"] for item in snapshot.json()["memory_versions"]
                }

        _stop_owned_process(process_a, port)
        process_a = None

        with process_b_log.open("w", encoding="utf-8") as log_b:
            process_b = _start_api(port=port, environment=environment, log=log_b)
            _wait_for_live(base_url, process_b)
            async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
                restored_memory = await client.get(f"/api/v1/memories/{memory_id}")
                assert restored_memory.status_code == 200
                assert restored_memory.json()["id"] == memory_id
                assert [
                    item["id"] for item in restored_memory.json()["evidence"]
                ] == evidence_ids
                restored_candidate = await client.get(
                    f"/api/v1/memory-candidates/{candidate_id}"
                )
                assert restored_candidate.status_code == 200
                assert restored_candidate.json()["id"] == candidate_id
                assert restored_candidate.json()["status"] == "ACCEPTED"
                restored_snapshot = await client.get(
                    f"/api/v1/context-snapshots/{snapshot_id}"
                )
                assert restored_snapshot.status_code == 200
                assert restored_snapshot.json()["id"] == snapshot_id
                assert restored_snapshot.json()["context_fingerprint"] == fingerprint
                restored_plan = await client.get(f"/api/v1/plans/{plan_id}")
                assert restored_plan.status_code == 200
                assert (
                    restored_plan.json()["generation_metadata"][
                        "context_snapshot_reference_id"
                    ]
                    == snapshot_id
                )
                assert (
                    restored_plan.json()["generation_metadata"]["context_fingerprint"]
                    == fingerprint
                )
                ready = await client.get("/health/ready")
                assert ready.status_code == 200
                if redis_mode == "normal":
                    assert ready.json()["checks"]["redis"] == "ok"
                else:
                    assert ready.json()["status"] == "degraded"
                    assert ready.json()["checks"]["redis"] == "unavailable"
    finally:
        if process_b is not None:
            _stop_owned_process(process_b, port)
        if process_a is not None:
            _stop_owned_process(process_a, port)
        await _cleanup_user(mysql_test_database, user_id)
        if redis_mode == "normal" and user_id is not None:
            cache_manager = RedisManager(
                Settings(
                    app_env="test",
                    persistence_backend=PersistenceBackend.MYSQL,
                    database_url=SecretStr(mysql_test_url),
                    redis_enabled=True,
                    redis_url=SecretStr(test_redis_url),
                    redis_key_prefix=prefix,
                    _env_file=None,
                )
            )
            cache_key = cache_manager.build_key("memory", "active", "v1", str(user_id))
            await cache_manager.get_client().delete(cache_key)
            await cache_manager.close()

    logs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (process_a_log, process_b_log)
        if path.exists()
    )
    redis_password = urlsplit(test_redis_url).password
    credential_leaked = redis_password is not None and redis_password in logs
    assert not credential_leaked, "Redis credential leaked to an owned Uvicorn log"
    assert "Traceback" not in logs
    assert _port_is_free(port)
