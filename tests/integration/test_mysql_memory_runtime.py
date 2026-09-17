"""MySQL runtime composition for the durable Memory and Context services."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete

import app.main as main_module
from app.config import PersistenceBackend, Settings
from app.domain.context.enums import AgentType
from app.domain.context.models import ContextBuildCommand
from app.persistence.database import Database
from app.persistence.mysql.memory_repository import MySQLMemoryRepository
from app.persistence.mysql.models import (
    AuditEventModel,
    ContextSnapshotModel,
    IdempotencyRecordModel,
    MemoryCandidateModel,
    MemoryEvidenceModel,
    MemoryItemModel,
    UserAccountModel,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_lifespan_wires_memory_api_and_context_snapshot(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API and Context use the lifespan-owned MySQL services, not memory mode."""

    email = f"memory-runtime-{uuid4().hex}@example.test"
    settings = Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(mysql_test_url),
        redis_enabled=False,
        context_debug_api_enabled=True,
        single_user_email=email,
        _env_file=None,
    )
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    application = main_module.create_application()
    user_id: UUID | None = None
    memory_id: UUID | None = None
    candidate_id: UUID | None = None
    snapshot_id: UUID | None = None
    now = datetime.now(UTC)
    try:
        async with application.router.lifespan_context(application):
            assert isinstance(
                application.state.memory_repository, MySQLMemoryRepository
            )
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                user = await client.get("/api/v1/users/me")
                user_id = UUID(user.json()["id"])
                candidate = await client.post(
                    "/api/v1/memory-candidates",
                    json={
                        "client_request_id": "runtime-candidate-create",
                        "memory_type": "PREFERRED_LOCATION",
                        "key": "preferred_location",
                        "value": "home",
                        "source": "PROFILE_AGENT_CANDIDATE",
                        "source_reference": "runtime:test",
                        "evidence_summary": "Structured runtime test evidence.",
                        "confidence": None,
                        "expires_at": (now + timedelta(days=1)).isoformat(),
                    },
                )
                assert candidate.status_code == 201
                candidate_id = UUID(candidate.json()["id"])
                accepted = await client.post(
                    f"/api/v1/memory-candidates/{candidate_id}/accept",
                    json={
                        "client_request_id": "runtime-candidate-accept",
                        "expected_candidate_version": 1,
                        "confirmed_value": "home",
                        "valid_until": None,
                    },
                )
                assert accepted.status_code == 200
                memory_id = UUID(accepted.json()["memory"]["id"])
                listed = await client.get("/api/v1/memories")
                assert listed.status_code == 200
                assert [item["id"] for item in listed.json()] == [str(memory_id)]

            context_service = application.state.context_application_service
            snapshot = await context_service.build_snapshot(
                application.state.local_user,
                ContextBuildCommand(
                    agent_type=AgentType.PLAN_GENERATION,
                    current_task={"goal": "build a safe plan"},
                    recent_behavior_summary=(),
                    catalog_reference=None,
                    max_characters=None,
                ),
                scope_id="runtime-test-scope",
            )
            restored = await context_service.get_snapshot(
                application.state.local_user, snapshot.reference.id
            )
            assert restored.reference == snapshot.reference
            snapshot_id = snapshot.reference.id

        restarted = main_module.create_application()
        async with restarted.router.lifespan_context(restarted):
            async with AsyncClient(
                transport=ASGITransport(app=restarted), base_url="http://test"
            ) as client:
                memories = await client.get("/api/v1/memories")
                assert memories.status_code == 200
                assert [item["id"] for item in memories.json()] == [str(memory_id)]
            recovered = await restarted.state.context_application_service.get_snapshot(
                restarted.state.local_user, snapshot_id
            )
            assert recovered.reference.id == snapshot_id
    finally:
        if user_id is not None:
            async with mysql_test_database.session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(AuditEventModel).where(
                            AuditEventModel.user_id == str(user_id)
                        )
                    )
                    await session.execute(
                        delete(ContextSnapshotModel).where(
                            ContextSnapshotModel.user_id == str(user_id)
                        )
                    )
                    if memory_id is not None:
                        await session.execute(
                            delete(MemoryEvidenceModel).where(
                                MemoryEvidenceModel.memory_id == str(memory_id)
                            )
                        )
                        await session.execute(
                            delete(MemoryItemModel).where(
                                MemoryItemModel.id == str(memory_id)
                            )
                        )
                    if candidate_id is not None:
                        await session.execute(
                            delete(MemoryCandidateModel).where(
                                MemoryCandidateModel.id == str(candidate_id)
                            )
                        )
                    await session.execute(
                        delete(IdempotencyRecordModel).where(
                            IdempotencyRecordModel.user_id == str(user_id)
                        )
                    )
                    await session.execute(
                        delete(UserAccountModel).where(
                            UserAccountModel.id == str(user_id)
                        )
                    )
