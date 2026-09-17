"""Durable Session Design lifecycle through rebuilt MySQL app lifespans."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select

import app.main as main_module
from app.config import PersistenceBackend, Settings
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AuditEventModel,
    ContextSnapshotModel,
    FitnessProfileModel,
    IdempotencyRecordModel,
    SessionDesignApplicationResultModel,
    SessionDesignCandidateSetModel,
    SessionDesignCandidateSlotModel,
    SessionDesignDraftModel,
    SessionDesignTraceModel,
    SessionExerciseModel,
    UserAccountModel,
    UserConstraintModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)
from app.persistence.mysql.session_design_repository import (
    MySQLSessionDesignRepository,
)


def _next_monday() -> datetime:
    now = datetime.now(UTC)
    days = (7 - now.weekday()) % 7
    if days == 0:
        days = 7
    return datetime.combine(
        (now + timedelta(days=days)).date(),
        time(hour=10),
        tzinfo=UTC,
    )


def _generation_payload(token: str) -> dict[str, object]:
    monday = _next_monday()
    starts = (monday, monday + timedelta(days=2))
    return {
        "client_request_id": f"session-design-plan-{token}",
        "week_start": monday.date().isoformat(),
        "availability_slots": [
            {
                "start": value.isoformat(),
                "end": (value + timedelta(minutes=60)).isoformat(),
                "location_type": "HOME",
            }
            for value in starts
        ],
        "preferred_locations": ["HOME"],
        "preferred_session_types": [],
    }


async def _cleanup(database: Database, user_id: UUID | None) -> None:
    if user_id is None:
        return
    owner = str(user_id)
    async with database.session_factory() as session:
        async with session.begin():
            candidate_ids = (
                await session.scalars(
                    select(SessionDesignCandidateSetModel.id).where(
                        SessionDesignCandidateSetModel.user_id == owner
                    )
                )
            ).all()
            await session.execute(
                delete(SessionDesignApplicationResultModel).where(
                    SessionDesignApplicationResultModel.user_id == owner
                )
            )
            draft_ids = (
                await session.scalars(
                    select(SessionDesignDraftModel.id).where(
                        SessionDesignDraftModel.user_id == owner
                    )
                )
            ).all()
            if draft_ids:
                await session.execute(
                    delete(SessionDesignTraceModel).where(
                        SessionDesignTraceModel.draft_id.in_(draft_ids)
                    )
                )
            await session.execute(
                delete(SessionDesignDraftModel).where(
                    SessionDesignDraftModel.user_id == owner
                )
            )
            if candidate_ids:
                await session.execute(
                    delete(SessionDesignCandidateSlotModel).where(
                        SessionDesignCandidateSlotModel.candidate_set_id.in_(
                            candidate_ids
                        )
                    )
                )
            await session.execute(
                delete(SessionDesignCandidateSetModel).where(
                    SessionDesignCandidateSetModel.user_id == owner
                )
            )
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id == owner)
            )
            await session.execute(
                delete(ContextSnapshotModel).where(
                    ContextSnapshotModel.user_id == owner
                )
            )
            await session.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.user_id == owner
                )
            )
            plan_ids = (
                await session.scalars(
                    select(WeeklyPlanModel.id).where(WeeklyPlanModel.user_id == owner)
                )
            ).all()
            if plan_ids:
                physical_session_ids = (
                    await session.scalars(
                        select(WorkoutSessionModel.id).where(
                            WorkoutSessionModel.plan_id.in_(plan_ids)
                        )
                    )
                ).all()
                if physical_session_ids:
                    await session.execute(
                        delete(SessionExerciseModel).where(
                            SessionExerciseModel.session_id.in_(physical_session_ids)
                        )
                    )
                await session.execute(
                    delete(WorkoutSessionModel).where(
                        WorkoutSessionModel.plan_id.in_(plan_ids)
                    )
                )
            await session.execute(
                delete(WeeklyPlanModel).where(WeeklyPlanModel.user_id == owner)
            )
            profile_ids = (
                await session.scalars(
                    select(FitnessProfileModel.id).where(
                        FitnessProfileModel.user_id == owner
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
                delete(FitnessProfileModel).where(FitnessProfileModel.user_id == owner)
            )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id == owner)
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_session_design_draft_apply_survives_lifespan_rebuilds(
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
        single_user_email=f"session-design-runtime-{token}@fitweek.test",
        _env_file=None,
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    user_id: UUID | None = None
    draft_id = ""
    candidate_set_id = ""
    snapshot_id = ""
    plan_id = ""
    target_session_id = ""
    try:
        app_a = main_module.create_application()
        async with app_a.router.lifespan_context(app_a):
            assert isinstance(
                app_a.state.session_design_repository,
                MySQLSessionDesignRepository,
            )
            async with AsyncClient(
                transport=ASGITransport(app=app_a),
                base_url="http://test",
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
                confirmed_plan = confirmed.json()
                plan_id = confirmed_plan["id"]
                target = confirmed_plan["sessions"][0]
                target_session_id = target["id"]
                created = await client.post(
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
                assert created.status_code == 201, created.text
                draft = created.json()
                draft_id = draft["id"]
                candidate_set_id = draft["candidate_set_id"]
                snapshot_id = draft["context_snapshot_reference_id"]
                reused = await client.post(
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
                assert reused.status_code == 200
                assert reused.json()["id"] == draft_id
                assert reused.json()["candidate_set_id"] == candidate_set_id
                assert reused.json()["context_snapshot_reference_id"] == snapshot_id

        app_b = main_module.create_application()
        async with app_b.router.lifespan_context(app_b):
            async with AsyncClient(
                transport=ASGITransport(app=app_b),
                base_url="http://test",
            ) as client:
                restored = await client.get(f"/api/v1/session-designs/{draft_id}")
                assert restored.status_code == 200, restored.text
                assert restored.json()["candidate_set_id"] == candidate_set_id
                accepted = await client.post(
                    f"/api/v1/session-designs/{draft_id}/accept",
                    json={"expected_version": restored.json()["version"]},
                )
                assert accepted.status_code == 200, accepted.text
                accept_body = accepted.json()
                assert accept_body["status"] == "ACCEPTED"
                source = await client.get(f"/api/v1/plans/{plan_id}")
                source_body = source.json()
                apply_payload = {
                    "client_request_id": f"session-design-apply-{token}",
                    "expected_draft_version": accept_body["version"],
                    "root_plan_id": plan_id,
                    "source_revision": source_body["revision"],
                    "expected_plan_version": source_body["version"],
                    "target_session_id": target_session_id,
                }
                preview = await client.post(
                    f"/api/v1/session-designs/{draft_id}/apply-preview",
                    json=apply_payload,
                )
                assert preview.status_code == 200, preview.text
                applied = await client.post(
                    f"/api/v1/session-designs/{draft_id}/apply",
                    json=apply_payload,
                )
                assert applied.status_code == 201, applied.text
                result = applied.json()
                assert result["draft"]["status"] == "APPLIED"
                assert result["plan"]["status"] == "VALIDATED"
                assert result["plan"]["revision"] == 2
                assert result["plan"]["sessions"][0]["id"] == target_session_id

        app_c = main_module.create_application()
        async with app_c.router.lifespan_context(app_c):
            async with AsyncClient(
                transport=ASGITransport(app=app_c),
                base_url="http://test",
            ) as client:
                restored_draft = await client.get(f"/api/v1/session-designs/{draft_id}")
                assert restored_draft.status_code == 200
                assert restored_draft.json()["status"] == "APPLIED"
                result = await client.get(
                    f"/api/v1/session-designs/{draft_id}/application-result"
                )
                assert result.status_code == 200, result.text
                assert result.json()["plan"]["revision"] == 2
                revisions = await client.get(f"/api/v1/plans/{plan_id}/revisions")
                assert revisions.status_code == 200
                assert [item["is_current_revision"] for item in revisions.json()] == [
                    True,
                    False,
                ]
                confirmed_revision = await client.post(
                    f"/api/v1/plans/{plan_id}/revisions/2/confirm",
                    json={"expected_version": 1},
                )
                assert confirmed_revision.status_code == 200, confirmed_revision.text
                assert confirmed_revision.json()["is_current_revision"] is True
    finally:
        await _cleanup(mysql_test_database, user_id)
