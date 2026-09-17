"""Three-process recovery for Session Design Drafts and applications."""

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
from tests.integration.test_mysql_session_design_draft_runtime import (
    _cleanup,
    _generation_payload,
)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("restart_iteration", range(3))
async def test_mysql_session_design_survives_three_uvicorn_processes(
    tmp_path: Path,
    mysql_test_database: Database,
    mysql_test_url: str,
    restart_iteration: int,
) -> None:
    token = f"{restart_iteration}-{uuid4().hex}"
    port = _free_port()
    api_url = f"http://127.0.0.1:{port}"
    email = f"session-design-uvicorn-{token}@fitweek.test"
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "PERSISTENCE_BACKEND": "mysql",
            "DATABASE_URL": mysql_test_url,
            "SINGLE_USER_EMAIL": email,
            "REDIS_ENABLED": "false",
            "MODEL_GATEWAY_ENABLED": "false",
            "ORCHESTRATOR_ENABLED": "false",
            "CONTEXT_DEBUG_API_ENABLED": "false",
        }
    )
    process_a: subprocess.Popen[object] | None = None
    process_b: subprocess.Popen[object] | None = None
    process_c: subprocess.Popen[object] | None = None
    user_id: UUID | None = None
    logs = tuple(
        tmp_path / f"session-design-{name}-{restart_iteration}.log"
        for name in ("a", "b", "c")
    )
    try:
        with logs[0].open("w", encoding="utf-8") as handle:
            process_a = _start_uvicorn(
                "app.main:app",
                port=port,
                environment=environment,
                log=handle,
            )
            _wait_for_url(f"{api_url}/health/live", process_a, label="API A")
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
                    "/api/v1/plans/generate",
                    json=_generation_payload(token),
                )
                assert generated.status_code == 201, generated.text
                plan = generated.json()["plan"]
                confirmed = await client.post(
                    f"/api/v1/plans/{plan['id']}/confirm",
                    json={"expected_version": plan["version"]},
                )
                assert confirmed.status_code == 200, confirmed.text
                source = confirmed.json()
                plan_id = source["id"]
                target = source["sessions"][0]
                target_session_id = target["id"]
                draft_response = await client.post(
                    "/api/v1/session-designs",
                    json={
                        "client_request_id": f"session-design-{token}",
                        "target_date": target["scheduled_start"][:10],
                        "target_duration_minutes": target["estimated_minutes"],
                        "location": target["location_type"],
                        "goal": "GENERAL_FITNESS",
                        "preferred_session_type": target["session_type"],
                    },
                )
                assert draft_response.status_code == 201, draft_response.text
                draft = draft_response.json()
                draft_id = draft["id"]
                candidate_set_id = draft["candidate_set_id"]
                snapshot_id = draft["context_snapshot_reference_id"]
                context_fingerprint = draft["context_fingerprint"]
                source_version = source["version"]
        _stop_owned_process(process_a, port)
        process_a = None

        with logs[1].open("w", encoding="utf-8") as handle:
            process_b = _start_uvicorn(
                "app.main:app",
                port=port,
                environment=environment,
                log=handle,
            )
            _wait_for_url(f"{api_url}/health/live", process_b, label="API B")
            async with httpx.AsyncClient(base_url=api_url, timeout=8) as client:
                restored = await client.get(f"/api/v1/session-designs/{draft_id}")
                assert restored.status_code == 200, restored.text
                restored_body = restored.json()
                assert restored_body["candidate_set_id"] == candidate_set_id
                assert restored_body["context_snapshot_reference_id"] == snapshot_id
                assert restored_body["context_fingerprint"] == context_fingerprint
                accepted = await client.post(
                    f"/api/v1/session-designs/{draft_id}/accept",
                    json={"expected_version": restored_body["version"]},
                )
                assert accepted.status_code == 200, accepted.text
                application_request_id = f"session-design-apply-{token}"
                application = {
                    "client_request_id": application_request_id,
                    "expected_draft_version": accepted.json()["version"],
                    "root_plan_id": plan_id,
                    "source_revision": 1,
                    "expected_plan_version": source_version,
                    "target_session_id": target_session_id,
                }
                applied = await client.post(
                    f"/api/v1/session-designs/{draft_id}/apply",
                    json=application,
                )
                assert applied.status_code == 201, applied.text
                applied_body = applied.json()
                result_id = applied_body["result"]["id"]
                revision_id = applied_body["plan"]["id"]
                assert applied_body["plan"]["status"] == "VALIDATED"
                assert applied_body["plan"]["sessions"][0]["id"] == target_session_id
                repeated = await client.post(
                    f"/api/v1/session-designs/{draft_id}/apply",
                    json=application,
                )
                assert repeated.status_code == 200, repeated.text
                assert repeated.json()["result"]["id"] == result_id
        _stop_owned_process(process_b, port)
        process_b = None

        with logs[2].open("w", encoding="utf-8") as handle:
            process_c = _start_uvicorn(
                "app.main:app",
                port=port,
                environment=environment,
                log=handle,
            )
            _wait_for_url(f"{api_url}/health/live", process_c, label="API C")
            async with httpx.AsyncClient(base_url=api_url, timeout=8) as client:
                draft = await client.get(f"/api/v1/session-designs/{draft_id}")
                assert draft.status_code == 200
                assert draft.json()["status"] == "APPLIED"
                result = await client.get(
                    f"/api/v1/session-designs/{draft_id}/application-result"
                )
                assert result.status_code == 200, result.text
                assert result.json()["result"]["id"] == result_id
                assert result.json()["plan"]["id"] == revision_id
                revisions = await client.get(f"/api/v1/plans/{plan_id}/revisions")
                assert revisions.status_code == 200
                assert [item["is_current_revision"] for item in revisions.json()] == [
                    True,
                    False,
                ]
    finally:
        if process_c is not None:
            _stop_owned_process(process_c, port)
        if process_b is not None:
            _stop_owned_process(process_b, port)
        if process_a is not None:
            _stop_owned_process(process_a, port)
        await _cleanup(mysql_test_database, user_id)

    combined_logs = "\n".join(
        path.read_text(encoding="utf-8") for path in logs if path.exists()
    )
    password = urlsplit(mysql_test_url).password
    assert password is None or password not in combined_logs
    assert "Traceback" not in combined_logs
    assert _port_is_free(port)
