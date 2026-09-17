"""MySQL lifespan integration for plan confirmation and check-ins."""

from uuid import uuid4

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
    ProfileDraftModel,
    SessionCheckinModel,
    SessionExerciseModel,
    UserAccountModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)
from tests.api.helpers import generation_payload, plan_payload, profile_payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_plan_confirm_and_checkin_are_readable_after_lifespan_restart(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = f"plan-api-{uuid4().hex}@fitweek.test"
    settings = Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(mysql_test_url),
        redis_enabled=False,
        single_user_email=email,
        _env_file=None,
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    application = main_module.create_application()
    user_id: str | None = None
    plan_id: str | None = None
    try:
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                user_id = (await client.get("/api/v1/users/me")).json()["id"]
                profile_context = await client.post(
                    "/api/v1/profile-agent/parse",
                    json={
                        "client_request_id": f"profile-context-{uuid4().hex}",
                        "user_message": (
                            "I can train at home twice a week for 30 minutes "
                            "with resistance bands."
                        ),
                        "current_week": "2026-07-20",
                    },
                )
                assert profile_context.status_code == 201, profile_context.text
                assert (
                    await client.put("/api/v1/profiles/me", json=profile_payload())
                ).status_code == 200
                generated = await client.post(
                    "/api/v1/plans/generate",
                    json={
                        **generation_payload(),
                        "client_request_id": f"generate-{uuid4().hex}",
                    },
                )
                assert generated.status_code == 201, generated.text
                async with mysql_test_database.session_factory() as session:
                    snapshot_agents = set(
                        (
                            await session.scalars(
                                select(ContextSnapshotModel.agent_type).where(
                                    ContextSnapshotModel.user_id == user_id
                                )
                            )
                        ).all()
                    )
                    assert snapshot_agents >= {"PROFILE_AGENT", "PLAN_GENERATION"}
                created = await client.post("/api/v1/plans", json=plan_payload())
                assert created.status_code == 201
                created_plan = created.json()["plan"]
                plan_id = created_plan["id"]
                confirmed = await client.post(
                    f"/api/v1/plans/{plan_id}/confirm",
                    json={"expected_version": created_plan["version"]},
                )
                assert confirmed.status_code == 200, confirmed.text
                session_id = confirmed.json()["sessions"][0]["id"]
                listed_sessions = await client.get(f"/api/v1/plans/{plan_id}/sessions")
                assert listed_sessions.status_code == 200
                assert [item["id"] for item in listed_sessions.json()] == [
                    item["id"] for item in confirmed.json()["sessions"]
                ]
                checkin = await client.post(
                    f"/api/v1/sessions/{session_id}/check-ins",
                    json={
                        "client_event_id": f"checkin-{uuid4().hex}",
                        "status": "COMPLETED",
                        "actual_minutes": 30,
                        "perceived_effort": 5,
                        "note": None,
                        "occurred_at": "2026-07-20T10:30:00Z",
                    },
                )
                assert checkin.status_code == 201
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                restored = await client.get(f"/api/v1/plans/{plan_id}")
                assert restored.status_code == 200
                assert restored.json()["status"] == "CONFIRMED"
    finally:
        if user_id is not None:
            async with mysql_test_database.session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(AuditEventModel).where(
                            AuditEventModel.user_id == user_id
                        )
                    )
                    await session.execute(
                        delete(ProfileDraftModel).where(
                            ProfileDraftModel.user_id == user_id
                        )
                    )
                    await session.execute(
                        delete(ContextSnapshotModel).where(
                            ContextSnapshotModel.user_id == user_id
                        )
                    )
                    await session.execute(
                        delete(SessionCheckinModel).where(
                            SessionCheckinModel.user_id == user_id
                        )
                    )
                    await session.execute(
                        delete(IdempotencyRecordModel).where(
                            IdempotencyRecordModel.user_id == user_id
                        )
                    )
                    plan_ids = (
                        await session.scalars(
                            WeeklyPlanModel.__table__.select()
                            .with_only_columns(WeeklyPlanModel.id)
                            .where(WeeklyPlanModel.user_id == user_id)
                        )
                    ).all()
                    if plan_ids:
                        session_ids = (
                            await session.scalars(
                                WorkoutSessionModel.__table__.select()
                                .with_only_columns(WorkoutSessionModel.id)
                                .where(WorkoutSessionModel.plan_id.in_(plan_ids))
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
                        delete(WeeklyPlanModel).where(
                            WeeklyPlanModel.user_id == user_id
                        )
                    )
                    await session.execute(
                        delete(FitnessProfileModel).where(
                            FitnessProfileModel.user_id == user_id
                        )
                    )
                    await session.execute(
                        delete(UserAccountModel).where(UserAccountModel.id == user_id)
                    )
