"""MySQL Profile/Plan Context reference, reuse, and priority semantics."""

from datetime import UTC, datetime, timedelta
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
    MemoryCandidateModel,
    MemoryEvidenceModel,
    MemoryItemModel,
    ProfileDraftModel,
    SessionExerciseModel,
    UserAccountModel,
    UserConstraintModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)
from tests.api.helpers import generation_payload, profile_payload


def _settings(mysql_test_url: str, email: str) -> Settings:
    return Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(mysql_test_url),
        redis_enabled=False,
        single_user_email=email,
        _env_file=None,
    )


async def _accept_memory(
    client: AsyncClient,
    *,
    memory_type: str,
    key: str,
    value: str,
) -> UUID:
    request_key = uuid4().hex
    created = await client.post(
        "/api/v1/memory-candidates",
        json={
            "client_request_id": f"candidate-create-{request_key}",
            "memory_type": memory_type,
            "key": key,
            "value": value,
            "source": "PROFILE_AGENT_CANDIDATE",
            "source_reference": "profile-plan-context-semantics",
            "evidence_summary": "Structured semantic integration evidence.",
            "confidence": None,
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    accepted = await client.post(
        f"/api/v1/memory-candidates/{created.json()['id']}/accept",
        json={
            "client_request_id": f"candidate-accept-{request_key}",
            "expected_candidate_version": 1,
            "confirmed_value": value,
            "valid_until": None,
        },
    )
    assert accepted.status_code == 200, accepted.text
    return UUID(accepted.json()["memory"]["id"])


async def _cleanup_user(database: Database, user_id: UUID | None) -> None:
    if user_id is None:
        return
    async with database.session_factory() as session:
        async with session.begin():
            plan_ids = (
                await session.scalars(
                    select(WeeklyPlanModel.id).where(
                        WeeklyPlanModel.user_id == str(user_id)
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
                delete(WeeklyPlanModel).where(WeeklyPlanModel.user_id == str(user_id))
            )
            memory_ids = (
                await session.scalars(
                    select(MemoryItemModel.id).where(
                        MemoryItemModel.user_id == str(user_id)
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
                delete(AuditEventModel).where(AuditEventModel.user_id == str(user_id))
            )
            await session.execute(
                delete(ProfileDraftModel).where(
                    ProfileDraftModel.user_id == str(user_id)
                )
            )
            await session.execute(
                delete(ContextSnapshotModel).where(
                    ContextSnapshotModel.user_id == str(user_id)
                )
            )
            await session.execute(
                delete(MemoryCandidateModel).where(
                    MemoryCandidateModel.user_id == str(user_id)
                )
            )
            await session.execute(
                delete(MemoryItemModel).where(MemoryItemModel.user_id == str(user_id))
            )
            await session.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.user_id == str(user_id)
                )
            )
            profile_ids = select(FitnessProfileModel.id).where(
                FitnessProfileModel.user_id == str(user_id)
            )
            await session.execute(
                delete(UserConstraintModel).where(
                    UserConstraintModel.profile_id.in_(profile_ids)
                )
            )
            await session.execute(
                delete(FitnessProfileModel).where(
                    FitnessProfileModel.user_id == str(user_id)
                )
            )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id == str(user_id))
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_profile_and_plan_persist_and_reuse_snapshot_references(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = f"profile-plan-context-{uuid4().hex}@fitweek.test"
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(
        main_module, "get_settings", lambda: _settings(mysql_test_url, email)
    )
    application = main_module.create_application()
    user_id: UUID | None = None
    try:
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
                assert (
                    await client.put("/api/v1/profiles/me", json=profile_payload())
                ).status_code == 200

                parse_payload = {
                    "client_request_id": "profile-context-reuse",
                    "user_message": "I can train at home twice a week for 30 minutes.",
                    "current_week": "2026-07-20",
                }
                first_profile = await client.post(
                    "/api/v1/profile-agent/parse", json=parse_payload
                )
                second_profile = await client.post(
                    "/api/v1/profile-agent/parse", json=parse_payload
                )
                assert first_profile.status_code == 201, first_profile.text
                assert second_profile.status_code == 200, second_profile.text
                profile_reference = first_profile.json()
                assert second_profile.json()["id"] == profile_reference["id"]
                assert (
                    second_profile.json()["context_snapshot_reference_id"]
                    == profile_reference["context_snapshot_reference_id"]
                )
                assert profile_reference["context_fingerprint"]
                assert profile_reference["context_contract_version"]
                assert profile_reference["context_policy_version"]
                assert "context_data" not in first_profile.text
                profile_conflict = await client.post(
                    "/api/v1/profile-agent/parse",
                    json={
                        **parse_payload,
                        "user_message": "I need a different training schedule.",
                    },
                )
                assert profile_conflict.status_code == 409

                plan_payload = {
                    **generation_payload(),
                    "client_request_id": "plan-context-reuse",
                }
                first_plan = await client.post(
                    "/api/v1/plans/generate", json=plan_payload
                )
                second_plan = await client.post(
                    "/api/v1/plans/generate", json=plan_payload
                )
                assert first_plan.status_code == second_plan.status_code == 201
                generation = first_plan.json()["generation"]
                assert (
                    second_plan.json()["plan"]["id"] == first_plan.json()["plan"]["id"]
                )
                assert second_plan.json()["generation"] == generation
                assert generation["context_snapshot_reference_id"] != "none"
                assert generation["context_fingerprint"] != "none"
                assert generation["context_contract_version"] != "none"
                assert generation["context_policy_version"] != "none"
                assert generation["context_degradation_state"] == "NONE"
                plan_conflict = await client.post(
                    "/api/v1/plans/generate",
                    json={
                        **plan_payload,
                        "preferred_session_types": ["MOBILITY"],
                    },
                )
                assert plan_conflict.status_code == 409

                persisted = await client.get(
                    f"/api/v1/plans/{first_plan.json()['plan']['id']}"
                )
                assert persisted.status_code == 200
                assert persisted.json()["generation_metadata"] == generation
                snapshot = (
                    await application.state.context_application_service.get_snapshot(
                        application.state.local_user,
                        UUID(generation["context_snapshot_reference_id"]),
                    )
                )
                assert (
                    snapshot.reference.context_fingerprint
                    == generation["context_fingerprint"]
                )
                async with mysql_test_database.session_factory() as session:
                    profile_snapshot_count = len(
                        (
                            await session.scalars(
                                select(ContextSnapshotModel.id).where(
                                    ContextSnapshotModel.user_id == str(user_id),
                                    ContextSnapshotModel.agent_type == "PROFILE_AGENT",
                                )
                            )
                        ).all()
                    )
                    plan_snapshot_count = len(
                        (
                            await session.scalars(
                                select(ContextSnapshotModel.id).where(
                                    ContextSnapshotModel.user_id == str(user_id),
                                    ContextSnapshotModel.agent_type
                                    == "PLAN_GENERATION",
                                )
                            )
                        ).all()
                    )
                    assert profile_snapshot_count == plan_snapshot_count == 1
    finally:
        await _cleanup_user(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_plan_context_is_frozen_and_hard_constraints_shadow_memory(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = f"profile-plan-frozen-{uuid4().hex}@fitweek.test"
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(
        main_module, "get_settings", lambda: _settings(mysql_test_url, email)
    )
    application = main_module.create_application()
    user_id: UUID | None = None
    try:
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
                profile = await client.put(
                    "/api/v1/profiles/me",
                    json=profile_payload(primary_goal="MOBILITY"),
                )
                assert profile.status_code == 200
                constraint = await client.post(
                    "/api/v1/profiles/me/constraints",
                    json={
                        "constraint_type": "ALLOWED_LOCATION",
                        "constraint_value": "HOME",
                        "priority": 100,
                        "is_hard": True,
                        "source": "USER_EXPLICIT",
                        "valid_until": None,
                    },
                )
                assert constraint.status_code == 201, constraint.text
                conflicting_memory = await _accept_memory(
                    client,
                    memory_type="PREFERRED_LOCATION",
                    key="preferred_location",
                    value="GYM",
                )
                first = await client.post(
                    "/api/v1/plans/generate",
                    json={
                        **generation_payload(),
                        "preferred_locations": [],
                        "client_request_id": "frozen-plan-scope",
                    },
                )
                assert first.status_code == 201, first.text
                first_generation = first.json()["generation"]
                first_snapshot = (
                    await application.state.context_application_service.get_snapshot(
                        application.state.local_user,
                        UUID(first_generation["context_snapshot_reference_id"]),
                    )
                )
                audit = await application.state.memory_repository.get_context_audit(
                    user_id, first_snapshot.reference.context_audit_id
                )
                assert audit is not None
                assert conflicting_memory in audit.excluded_memory_ids
                assert any(
                    item.higher_priority_source == "HARD_CONSTRAINTS"
                    and item.lower_priority_source == "MEMORY"
                    for item in audit.conflicts
                )
                assert {
                    session["location_type"]
                    for session in first.json()["plan"]["sessions"]
                } == {"HOME"}
                assert (
                    first.json()["plan"]["goal_snapshot"]["primary_goal"] == "MOBILITY"
                )

                later_memory = await _accept_memory(
                    client,
                    memory_type="TRAINING_STYLE_PREFERENCE",
                    key="training_style",
                    value="circuit",
                )
                retry = await client.post(
                    "/api/v1/plans/generate",
                    json={
                        **generation_payload(),
                        "preferred_locations": [],
                        "client_request_id": "frozen-plan-scope",
                    },
                )
                assert retry.status_code == 201, retry.text
                assert retry.json()["generation"] == first_generation
                reused = (
                    await application.state.context_application_service.get_snapshot(
                        application.state.local_user,
                        UUID(
                            retry.json()["generation"]["context_snapshot_reference_id"]
                        ),
                    )
                )
                assert reused.reference == first_snapshot.reference
                assert later_memory not in {
                    item.id for item in reused.reference.memory_versions
                }

                fresh = await client.post(
                    "/api/v1/plans/generate",
                    json={
                        **generation_payload(),
                        "preferred_locations": [],
                        "client_request_id": "fresh-plan-scope",
                    },
                )
                assert fresh.status_code == 201, fresh.text
                fresh_snapshot = (
                    await application.state.context_application_service.get_snapshot(
                        application.state.local_user,
                        UUID(
                            fresh.json()["generation"]["context_snapshot_reference_id"]
                        ),
                    )
                )
                assert fresh_snapshot.reference.id != first_snapshot.reference.id
                assert later_memory in {
                    item.id for item in fresh_snapshot.reference.memory_versions
                }

                profile_conflicting_memory = await _accept_memory(
                    client,
                    memory_type="TRAINING_STYLE_PREFERENCE",
                    key="primary_goal",
                    value="BASIC_STRENGTH",
                )
                profile_priority = await client.post(
                    "/api/v1/plans/generate",
                    json={
                        **generation_payload(),
                        "preferred_locations": [],
                        "client_request_id": "profile-priority-scope",
                    },
                )
                assert profile_priority.status_code == 201, profile_priority.text
                profile_snapshot = (
                    await application.state.context_application_service.get_snapshot(
                        application.state.local_user,
                        UUID(
                            profile_priority.json()["generation"][
                                "context_snapshot_reference_id"
                            ]
                        ),
                    )
                )
                profile_audit = (
                    await application.state.memory_repository.get_context_audit(
                        user_id, profile_snapshot.reference.context_audit_id
                    )
                )
                assert profile_audit is not None
                assert profile_conflicting_memory in profile_audit.excluded_memory_ids
                assert any(
                    item.higher_priority_source == "PROFILE"
                    and item.lower_priority_source == "MEMORY"
                    for item in profile_audit.conflicts
                )
                assert (
                    profile_priority.json()["plan"]["goal_snapshot"]["primary_goal"]
                    == "MOBILITY"
                )
    finally:
        await _cleanup_user(mysql_test_database, user_id)
