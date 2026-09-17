"""Three-process recovery for frozen Schedule Drafts and applications."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest

from app.persistence.database import Database
from tests.integration.test_mysql_profile_draft_uvicorn_restart import (
    _free_port,
    _port_is_free,
    _start_uvicorn,
    _stop_owned_process,
    _wait_for_url,
)
from tests.integration.test_mysql_schedule_draft_runtime import (
    _cleanup,
    _schedule_payload,
)
from tests.integration.test_mysql_session_design_draft_runtime import (
    _generation_payload,
)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("restart_iteration", range(3))
async def test_mysql_schedule_survives_three_uvicorn_processes(
    tmp_path: Path,
    mysql_test_database: Database,
    mysql_test_url: str,
    restart_iteration: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    token = f"{restart_iteration}-{uuid4().hex}"
    port = _free_port()
    api_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "PERSISTENCE_BACKEND": "mysql",
            "DATABASE_URL": mysql_test_url,
            "SINGLE_USER_EMAIL": f"schedule-uvicorn-{token}@fitweek.test",
            "REDIS_ENABLED": "false",
            "MODEL_GATEWAY_ENABLED": "false",
            "CALENDAR_READ_ENABLED": "false",
            "ORCHESTRATOR_ENABLED": "false",
            "CONTEXT_DEBUG_API_ENABLED": "false",
        }
    )
    processes: list[subprocess.Popen[object] | None] = [None, None, None]
    user_id: UUID | None = None
    logs = tuple(
        tmp_path / f"schedule-{name}-{restart_iteration}.log"
        for name in ("a", "b", "c")
    )
    try:
        with logs[0].open("w", encoding="utf-8") as handle:
            processes[0] = _start_uvicorn(
                "app.main:app", port=port, environment=environment, log=handle
            )
            _wait_for_url(f"{api_url}/health/live", processes[0], label="API A")
            async with httpx.AsyncClient(base_url=api_url, timeout=8) as client:
                user = await client.get("/api/v1/users/me")
                assert user.status_code == 200
                user_id = UUID(user.json()["id"])
                profile = await client.put(
                    "/api/v1/profiles/me",
                    json={
                        "experience_level": "BEGINNER",
                        "weekly_frequency": 2,
                        "max_session_minutes": 45,
                        "primary_goal": "GENERAL_FITNESS",
                        "scope_confirmed": True,
                    },
                )
                assert profile.status_code == 200, profile.text
                generated = await client.post(
                    "/api/v1/plans/generate", json=_generation_payload(token)
                )
                assert generated.status_code == 201, generated.text
                generated_plan = generated.json()["plan"]
                confirmed = await client.post(
                    f"/api/v1/plans/{generated_plan['id']}/confirm",
                    json={"expected_version": generated_plan["version"]},
                )
                assert confirmed.status_code == 200, confirmed.text
                source = confirmed.json()
                target = source["sessions"][0]
                created = await client.post(
                    "/api/v1/schedule-drafts",
                    json=_schedule_payload(token, source, target),
                )
                assert created.status_code == 201, created.text
                draft = created.json()
                draft_id = draft["id"]
                candidate_set_id = draft["candidate_set_id"]
                snapshot_id = draft["context_snapshot_reference_id"]
                context_fingerprint = draft["context_fingerprint"]
        _stop_owned_process(processes[0], port)
        processes[0] = None

        with logs[1].open("w", encoding="utf-8") as handle:
            processes[1] = _start_uvicorn(
                "app.main:app", port=port, environment=environment, log=handle
            )
            _wait_for_url(f"{api_url}/health/live", processes[1], label="API B")
            async with httpx.AsyncClient(base_url=api_url, timeout=8) as client:
                restored = await client.get(f"/api/v1/schedule-drafts/{draft_id}")
                assert restored.status_code == 200, restored.text
                restored_body = restored.json()
                assert restored_body["candidate_set_id"] == candidate_set_id
                assert restored_body["context_snapshot_reference_id"] == snapshot_id
                assert restored_body["context_fingerprint"] == context_fingerprint
                accepted = await client.post(
                    f"/api/v1/schedule-drafts/{draft_id}/accept",
                    json={"expected_version": restored_body["version"]},
                )
                assert accepted.status_code == 200, accepted.text
                apply_payload = {
                    "client_request_id": f"schedule-apply-{token}",
                    "expected_draft_version": accepted.json()["version"],
                    "root_plan_id": source["root_plan_id"] or source["id"],
                    "source_revision": source["revision"],
                    "expected_plan_version": source["version"],
                }
                applied = await client.post(
                    f"/api/v1/schedule-drafts/{draft_id}/apply",
                    json=apply_payload,
                )
                assert applied.status_code == 201, applied.text
                outcome = applied.json()
                result_id = outcome["result"]["id"]
                revision_id = outcome["plan"]["id"]
                assert outcome["plan"]["status"] == "VALIDATED"
                assert (
                    outcome["plan"]["sessions"][0]["exercises"] == target["exercises"]
                )
                repeated = await client.post(
                    f"/api/v1/schedule-drafts/{draft_id}/apply",
                    json=apply_payload,
                )
                assert repeated.status_code == 200, repeated.text
                assert repeated.json()["result"]["id"] == result_id
                assert repeated.json()["plan"]["id"] == revision_id
        _stop_owned_process(processes[1], port)
        processes[1] = None

        with logs[2].open("w", encoding="utf-8") as handle:
            processes[2] = _start_uvicorn(
                "app.main:app", port=port, environment=environment, log=handle
            )
            _wait_for_url(f"{api_url}/health/live", processes[2], label="API C")
            async with httpx.AsyncClient(base_url=api_url, timeout=8) as client:
                restored = await client.get(f"/api/v1/schedule-drafts/{draft_id}")
                assert restored.status_code == 200
                assert restored.json()["status"] == "APPLIED"
                result = await client.get(
                    f"/api/v1/schedule-drafts/{draft_id}/application-result"
                )
                assert result.status_code == 200, result.text
                assert result.json()["result"]["id"] == result_id
                assert result.json()["plan"]["id"] == revision_id
                revisions = await client.get(f"/api/v1/plans/{source['id']}/revisions")
                assert revisions.status_code == 200
                assert [item["is_current_revision"] for item in revisions.json()] == [
                    True,
                    False,
                ]
        _stop_owned_process(processes[2], port)
        processes[2] = None
    finally:
        for process in reversed(processes):
            if process is not None:
                _stop_owned_process(process, port)
        await _cleanup(mysql_test_database, user_id)

    combined_logs = "\n".join(
        path.read_text(encoding="utf-8") for path in logs if path.exists()
    )
    password = urlsplit(mysql_test_url).password
    assert password is None or password not in combined_logs
    assert "Traceback" not in combined_logs
    assert _port_is_free(port)
