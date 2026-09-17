"""MySQL ProfileDraft runtime, review transaction, and lifespan recovery."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete

import app.main as main_module
from app.config import PersistenceBackend, Settings
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
from app.persistence.mysql.profile_draft_repository import (
    MySQLProfileDraftRepository,
)


def _settings(mysql_test_url: str, email: str, stub_url: str) -> Settings:
    return Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(mysql_test_url),
        redis_enabled=False,
        model_gateway_enabled=True,
        model_primary_provider="http",
        model_primary_base_url=SecretStr(f"{stub_url}/profile-apply"),
        model_primary_api_key=SecretStr("local-profile-draft-key"),
        model_backup_provider="template-fallback",
        single_user_email=email,
        _env_file=None,
    )


@pytest.fixture
def profile_draft_stub_url() -> Iterator[str]:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = int(candidate.getsockname()[1])
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.stub_model_server.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/openapi.json", timeout=0.2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.03)
    else:
        process.terminate()
        process.wait(timeout=5)
        pytest.fail("Profile Draft model stub did not start")
    try:
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _parse_payload(token: str) -> dict[str, object]:
    return {
        "client_request_id": f"profile-draft-parse-{token}",
        "user_message": (
            "I can train at home three times per week for 30 minutes "
            "using resistance bands."
        ),
        "current_week": "2026-07-20",
    }


def _decision(token: str) -> dict[str, object]:
    return {
        "client_request_id": f"profile-draft-apply-{token}",
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
            profile_ids = [
                row
                for row in (
                    await session.scalars(
                        FitnessProfileModel.__table__.select()
                        .with_only_columns(FitnessProfileModel.id)
                        .where(FitnessProfileModel.user_id == user_id_text)
                    )
                ).all()
            ]
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
async def test_mysql_profile_draft_lifespan_parse_preview_apply_and_restore(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
    profile_draft_stub_url: str,
) -> None:
    token = uuid4().hex
    settings = _settings(
        mysql_test_url,
        f"profile-draft-runtime-{token}@fitweek.test",
        profile_draft_stub_url,
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    user_id: UUID | None = None
    draft_id: str | None = None
    snapshot_id: str | None = None
    fingerprint: str | None = None
    try:
        app_a = main_module.create_application()
        async with app_a.router.lifespan_context(app_a):
            assert isinstance(
                app_a.state.profile_draft_repository,
                MySQLProfileDraftRepository,
            )
            async with AsyncClient(
                transport=ASGITransport(app=app_a), base_url="http://test"
            ) as client:
                user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
                created = await client.post(
                    "/api/v1/profile-agent/parse", json=_parse_payload(token)
                )
                assert created.status_code == 201, created.text
                draft_id = created.json()["id"]
                snapshot_id = created.json()["context_snapshot_reference_id"]
                fingerprint = created.json()["context_fingerprint"]
                retried = await client.post(
                    "/api/v1/profile-agent/parse", json=_parse_payload(token)
                )
                assert retried.status_code == 200
                assert retried.json()["id"] == draft_id
                assert retried.json()["context_snapshot_reference_id"] == snapshot_id
                async with httpx.AsyncClient(
                    base_url=profile_draft_stub_url, timeout=2
                ) as stub_client:
                    call_count = await stub_client.get("/admin/count/profile-apply")
                assert call_count.json() == {"count": 1}
                conflicting_payload = {
                    **_parse_payload(token),
                    "user_message": (
                        "A different payload must not reuse this request id."
                    ),
                }
                conflict = await client.post(
                    "/api/v1/profile-agent/parse", json=conflicting_payload
                )
                assert conflict.status_code == 409
                assert conflict.json()["error"]["code"] == (
                    "PROFILE_AGENT_IDEMPOTENCY_CONFLICT"
                )

        app_b = main_module.create_application()
        async with app_b.router.lifespan_context(app_b):
            assert app_b.state.profile_draft_repository is not (
                app_a.state.profile_draft_repository
            )
            async with AsyncClient(
                transport=ASGITransport(app=app_b), base_url="http://test"
            ) as client:
                decision = _decision(token)
                detail = await client.get(f"/api/v1/profile-agent/drafts/{draft_id}")
                assert detail.status_code == 200
                assert detail.json()["context_snapshot_reference_id"] == snapshot_id
                assert detail.json()["context_fingerprint"] == fingerprint
                listed = await client.get("/api/v1/profile-agent/drafts")
                assert listed.status_code == 200
                assert [item["id"] for item in listed.json()] == [draft_id]
                missing = await client.get(f"/api/v1/profile-agent/drafts/{uuid4()}")
                assert missing.status_code == 404
                conflicting_decision = {
                    **_decision(token),
                    "expected_draft_version": 2,
                }
                version_conflict = await client.post(
                    f"/api/v1/profile-agent/drafts/{draft_id}/apply",
                    json=conflicting_decision,
                )
                assert version_conflict.status_code == 409
                assert version_conflict.json()["error"]["code"] == (
                    "PROFILE_DRAFT_VERSION_CONFLICT"
                )
                still_pending = await client.get(
                    f"/api/v1/profile-agent/drafts/{draft_id}"
                )
                assert still_pending.json()["status"] == "PENDING_REVIEW"
                assert (await client.get("/api/v1/profiles/me")).status_code == 404
                repository = app_b.state.profile_draft_repository
                original_audit = repository._audit

                def fail_audit(*_args: object, **_kwargs: object) -> None:
                    raise RuntimeError("controlled audit persistence failure")

                monkeypatch.setattr(repository, "_audit", fail_audit)
                try:
                    audit_failure = await client.post(
                        f"/api/v1/profile-agent/drafts/{draft_id}/apply",
                        json=_decision(token),
                    )
                finally:
                    monkeypatch.setattr(repository, "_audit", original_audit)
                assert audit_failure.status_code == 422
                assert audit_failure.json()["error"]["code"] == (
                    "PROFILE_DRAFT_APPLY_FAILED"
                )
                after_rollback = await client.get(
                    f"/api/v1/profile-agent/drafts/{draft_id}"
                )
                assert after_rollback.json()["status"] == "PENDING_REVIEW"
                assert (await client.get("/api/v1/profiles/me")).status_code == 404
                preview = await client.post(
                    f"/api/v1/profile-agent/drafts/{draft_id}/apply-preview",
                    json=decision,
                )
                assert preview.status_code == 200, preview.text
                assert preview.json()["validation"]["passed"] is True
                assert preview.json()["expires_at"] == detail.json()["expires_at"]
                assert preview.json()["context_snapshot_reference_id"] == snapshot_id
                assert preview.json()["context_fingerprint"] == fingerprint
                assert "context" not in preview.json()
                applied = await client.post(
                    f"/api/v1/profile-agent/drafts/{draft_id}/apply",
                    json=decision,
                )
                assert applied.status_code == 200, applied.text
                profile_id = applied.json()["profile_id"]
                repeated = await client.post(
                    f"/api/v1/profile-agent/drafts/{draft_id}/apply",
                    json=decision,
                )
                assert repeated.status_code == 200
                assert repeated.json() == applied.json()

                reject_token = f"{token}-reject"
                reject_created = await client.post(
                    "/api/v1/profile-agent/parse",
                    json=_parse_payload(reject_token),
                )
                assert reject_created.status_code == 201
                rejected_draft_id = reject_created.json()["id"]
                reject_payload = {
                    "client_request_id": f"profile-draft-reject-{token}",
                    "expected_draft_version": 1,
                }
                rejected = await client.post(
                    f"/api/v1/profile-agent/drafts/{rejected_draft_id}/reject",
                    json=reject_payload,
                )
                assert rejected.status_code == 200
                assert rejected.json()["created"] is True
                repeated_reject = await client.post(
                    f"/api/v1/profile-agent/drafts/{rejected_draft_id}/reject",
                    json=reject_payload,
                )
                assert repeated_reject.status_code == 200
                assert repeated_reject.json()["created"] is False
                rejected_apply = await client.post(
                    f"/api/v1/profile-agent/drafts/{rejected_draft_id}/apply",
                    json=_decision(reject_token),
                )
                assert rejected_apply.status_code == 409
                assert rejected_apply.json()["error"]["code"] == (
                    "PROFILE_DRAFT_REJECTED"
                )

        app_c = main_module.create_application()
        async with app_c.router.lifespan_context(app_c):
            async with AsyncClient(
                transport=ASGITransport(app=app_c), base_url="http://test"
            ) as client:
                restored_draft = await client.get(
                    f"/api/v1/profile-agent/drafts/{draft_id}"
                )
                assert restored_draft.status_code == 200
                assert restored_draft.json()["status"] == "APPLIED"
                assert restored_draft.json()["applied_profile_id"] == profile_id
                assert (
                    restored_draft.json()["context_snapshot_reference_id"]
                    == snapshot_id
                )
                restored_profile = await client.get("/api/v1/profiles/me")
                assert restored_profile.status_code == 200
                assert restored_profile.json()["id"] == profile_id
    finally:
        await _cleanup(mysql_test_database, user_id)
