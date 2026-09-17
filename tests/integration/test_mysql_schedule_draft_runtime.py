"""Durable Schedule Draft lifecycle through rebuilt MySQL app lifespans."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.main as main_module
from app.config import PersistenceBackend, Settings
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AuditEventModel,
    ScheduleApplicationResultModel,
    ScheduleAvailabilityWindowModel,
    ScheduleBusyIntervalModel,
    ScheduleBusySnapshotModel,
    ScheduleCandidateSetModel,
    ScheduleCandidateSlotModel,
    ScheduleDraftModel,
    ScheduleTraceModel,
)
from app.persistence.mysql.schedule_repository import MySQLScheduleDraftRepository
from tests.integration.test_mysql_session_design_draft_runtime import (
    _cleanup as _cleanup_business_data,
)
from tests.integration.test_mysql_session_design_draft_runtime import (
    _generation_payload,
)


async def _cleanup(database: Database, user_id: UUID | None) -> None:
    if user_id is None:
        return
    owner = str(user_id)
    async with database.session_factory() as session:
        async with session.begin():
            draft_ids = (
                await session.scalars(
                    select(ScheduleDraftModel.id).where(
                        ScheduleDraftModel.user_id == owner
                    )
                )
            ).all()
            candidate_ids = (
                await session.scalars(
                    select(ScheduleCandidateSetModel.id).where(
                        ScheduleCandidateSetModel.user_id == owner
                    )
                )
            ).all()
            busy_ids = (
                await session.scalars(
                    select(ScheduleBusySnapshotModel.id).where(
                        ScheduleBusySnapshotModel.user_id == owner
                    )
                )
            ).all()
            await session.execute(
                delete(ScheduleApplicationResultModel).where(
                    ScheduleApplicationResultModel.user_id == owner
                )
            )
            if draft_ids:
                await session.execute(
                    delete(ScheduleTraceModel).where(
                        ScheduleTraceModel.draft_id.in_(draft_ids)
                    )
                )
            await session.execute(
                delete(ScheduleDraftModel).where(ScheduleDraftModel.user_id == owner)
            )
            if candidate_ids:
                await session.execute(
                    delete(ScheduleCandidateSlotModel).where(
                        ScheduleCandidateSlotModel.candidate_set_id.in_(candidate_ids)
                    )
                )
                await session.execute(
                    delete(ScheduleAvailabilityWindowModel).where(
                        ScheduleAvailabilityWindowModel.candidate_set_id.in_(
                            candidate_ids
                        )
                    )
                )
            await session.execute(
                delete(ScheduleCandidateSetModel).where(
                    ScheduleCandidateSetModel.user_id == owner
                )
            )
            if busy_ids:
                await session.execute(
                    delete(ScheduleBusyIntervalModel).where(
                        ScheduleBusyIntervalModel.snapshot_id.in_(busy_ids)
                    )
                )
            await session.execute(
                delete(ScheduleBusySnapshotModel).where(
                    ScheduleBusySnapshotModel.user_id == owner
                )
            )
    await _cleanup_business_data(database, user_id)


def _schedule_payload(
    token: str, plan: dict[str, object], target: dict[str, object]
) -> dict[str, object]:
    original = datetime.fromisoformat(str(target["scheduled_start"]))
    proposed = original + timedelta(hours=2)
    return {
        "client_request_id": f"schedule-{token}",
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "timezone": "UTC",
        "availability_windows": [
            {
                "start": proposed.isoformat(),
                "end": (proposed + timedelta(hours=2)).isoformat(),
                "location": target["location_type"],
            }
        ],
        "manual_busy_windows": [],
        "target_session_ids": [target["id"]],
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_schedule_draft_apply_survives_lifespan_rebuilds(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = uuid4().hex
    settings = Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(mysql_test_url),
        redis_enabled=False,
        model_gateway_enabled=False,
        calendar_read_enabled=False,
        single_user_email=f"schedule-runtime-{token}@fitweek.test",
        _env_file=None,
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    user_id: UUID | None = None
    try:
        app_a = main_module.create_application()
        async with app_a.router.lifespan_context(app_a):
            assert isinstance(
                app_a.state.schedule_draft_repository,
                MySQLScheduleDraftRepository,
            )
            async with AsyncClient(
                transport=ASGITransport(app=app_a), base_url="http://test"
            ) as client:
                user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
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
                payload = _schedule_payload(token, source, target)
                created = await client.post("/api/v1/schedule-drafts", json=payload)
                assert created.status_code == 201, created.text
                draft = created.json()
                draft_id = draft["id"]
                candidate_set_id = draft["candidate_set_id"]
                snapshot_id = draft["context_snapshot_reference_id"]
                old_start = target["scheduled_start"]
                repeated = await client.post("/api/v1/schedule-drafts", json=payload)
                assert repeated.status_code == 200
                assert repeated.json()["id"] == draft_id
                assert repeated.json()["candidate_set_id"] == candidate_set_id

        app_b = main_module.create_application()
        async with app_b.router.lifespan_context(app_b):
            async with AsyncClient(
                transport=ASGITransport(app=app_b), base_url="http://test"
            ) as client:
                restored = await client.get(f"/api/v1/schedule-drafts/{draft_id}")
                assert restored.status_code == 200, restored.text
                restored_body = restored.json()
                assert restored_body["candidate_set_id"] == candidate_set_id
                assert restored_body["context_snapshot_reference_id"] == snapshot_id
                candidates = await client.get(
                    f"/api/v1/schedule-drafts/{draft_id}/candidate-set"
                )
                assert candidates.status_code == 200, candidates.text
                selected_slot_ids = {
                    item["slot_id"] for item in candidates.json()["slots"]
                }
                assert {
                    item["slot_id"] for item in restored_body["assignments"]
                } <= selected_slot_ids
                accepted = await client.post(
                    f"/api/v1/schedule-drafts/{draft_id}/accept",
                    json={"expected_version": restored_body["version"]},
                )
                assert accepted.status_code == 200, accepted.text
                accepted_body = accepted.json()
                source_plan = await client.get(f"/api/v1/plans/{source['id']}")
                before_apply = source_plan.json()
                apply_payload = {
                    "client_request_id": f"schedule-apply-{token}",
                    "expected_draft_version": accepted_body["version"],
                    "root_plan_id": source["root_plan_id"] or source["id"],
                    "source_revision": source["revision"],
                    "expected_plan_version": source["version"],
                }

                original_add = AsyncSession.add
                audit_failure_injected = False

                def fail_schedule_audit(
                    session: AsyncSession,
                    instance: object,
                    _warn: bool = True,
                ) -> None:
                    nonlocal audit_failure_injected
                    if (
                        isinstance(instance, AuditEventModel)
                        and instance.event_type == "SCHEDULE_DRAFT_APPLIED"
                    ):
                        audit_failure_injected = True
                        raise RuntimeError("controlled schedule Audit failure")
                    original_add(session, instance, _warn=_warn)

                monkeypatch.setattr(AsyncSession, "add", fail_schedule_audit)
                failed_apply = await client.post(
                    f"/api/v1/schedule-drafts/{draft_id}/apply",
                    json=apply_payload,
                )
                monkeypatch.setattr(AsyncSession, "add", original_add)
                assert audit_failure_injected
                assert failed_apply.status_code >= 400

                after_rollback = await client.get(f"/api/v1/schedule-drafts/{draft_id}")
                assert after_rollback.status_code == 200
                assert after_rollback.json()["status"] == "ACCEPTED"
                assert (
                    await client.get(
                        f"/api/v1/schedule-drafts/{draft_id}/application-result"
                    )
                ).status_code == 404
                rollback_revisions = await client.get(
                    f"/api/v1/plans/{source['id']}/revisions"
                )
                assert rollback_revisions.status_code == 200
                assert len(rollback_revisions.json()) == 1

                first_apply, second_apply = await asyncio.gather(
                    client.post(
                        f"/api/v1/schedule-drafts/{draft_id}/apply",
                        json=apply_payload,
                    ),
                    client.post(
                        f"/api/v1/schedule-drafts/{draft_id}/apply",
                        json=apply_payload,
                    ),
                )
                assert sorted((first_apply.status_code, second_apply.status_code)) == [
                    200,
                    201,
                ]
                first_outcome = first_apply.json()
                second_outcome = second_apply.json()
                assert first_outcome["result"]["id"] == second_outcome["result"]["id"]
                assert first_outcome["plan"]["id"] == second_outcome["plan"]["id"]
                outcome = first_outcome
                result_id = outcome["result"]["id"]
                result_plan_id = outcome["plan"]["id"]
                assert outcome["plan"]["status"] == "VALIDATED"
                changed = outcome["plan"]["sessions"][0]
                assert changed["scheduled_start"] != old_start
                assert changed["exercises"] == target["exercises"]
                assert changed["location_type"] == target["location_type"]
                assert changed["session_type"] == target["session_type"]
                assert changed["estimated_minutes"] == target["estimated_minutes"]
                assert before_apply["sessions"][0]["scheduled_start"] == old_start

        app_c = main_module.create_application()
        async with app_c.router.lifespan_context(app_c):
            async with AsyncClient(
                transport=ASGITransport(app=app_c), base_url="http://test"
            ) as client:
                draft = await client.get(f"/api/v1/schedule-drafts/{draft_id}")
                assert draft.status_code == 200
                assert draft.json()["status"] == "APPLIED"
                result = await client.get(
                    f"/api/v1/schedule-drafts/{draft_id}/application-result"
                )
                assert result.status_code == 200, result.text
                assert result.json()["result"]["id"] == result_id
                assert result.json()["plan"]["id"] == result_plan_id
                revisions = await client.get(f"/api/v1/plans/{source['id']}/revisions")
                assert revisions.status_code == 200
                assert [item["is_current_revision"] for item in revisions.json()] == [
                    True,
                    False,
                ]
    finally:
        await _cleanup(mysql_test_database, user_id)
