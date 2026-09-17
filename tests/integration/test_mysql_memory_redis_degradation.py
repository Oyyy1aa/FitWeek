"""Real MySQL fallback and isolated Redis cache-corruption coverage."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select

import app.api.health as health_module
import app.main as main_module
from app.config import PersistenceBackend, Settings, get_settings
from app.domain.context.enums import AgentType
from app.domain.context.models import ContextBuildCommand
from app.infrastructure.redis_client import RedisManager
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AuditEventModel,
    ContextSnapshotModel,
    IdempotencyRecordModel,
    MemoryCandidateModel,
    MemoryEvidenceModel,
    MemoryItemModel,
    UserAccountModel,
)


async def _accept_memory(
    client: AsyncClient, *, memory_type: str, key: str, value: str
) -> UUID:
    token = uuid4().hex
    candidate = await client.post(
        "/api/v1/memory-candidates",
        json={
            "client_request_id": f"redis-cache-create-{token}",
            "memory_type": memory_type,
            "key": key,
            "value": value,
            "source": "PROFILE_AGENT_CANDIDATE",
            "source_reference": "redis-cache-integration",
            "evidence_summary": "Structured cache integration evidence.",
            "confidence": None,
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert candidate.status_code == 201, candidate.text
    accepted = await client.post(
        f"/api/v1/memory-candidates/{candidate.json()['id']}/accept",
        json={
            "client_request_id": f"redis-cache-accept-{token}",
            "expected_candidate_version": 1,
            "confirmed_value": value,
            "valid_until": None,
        },
    )
    assert accepted.status_code == 200, accepted.text
    return UUID(accepted.json()["memory"]["id"])


async def _cleanup(database: Database, user_id: UUID | None) -> None:
    if user_id is None:
        return
    async with database.session_factory() as session:
        async with session.begin():
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
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id == str(user_id))
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_context_rebuilds_corrupt_isolated_redis_payload(
    mysql_test_database: Database,
    mysql_test_url: str,
    test_redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = f"fitweek:test:{uuid4().hex}"
    settings = Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(mysql_test_url),
        redis_enabled=True,
        redis_url=SecretStr(test_redis_url),
        redis_key_prefix=prefix,
        memory_cache_ttl_seconds=60,
        single_user_email=f"redis-cache-{uuid4().hex}@fitweek.test",
        _env_file=None,
    )
    manager = RedisManager(settings)
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "get_redis_manager", lambda: manager)
    application = main_module.create_application()
    user_id: UUID | None = None
    key: str | None = None
    unrelated_key: str | None = None
    try:
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
                first_memory = await _accept_memory(
                    client,
                    memory_type="PREFERRED_LOCATION",
                    key="preferred_location",
                    value="HOME",
                )
                second_memory = await _accept_memory(
                    client,
                    memory_type="PREFERRED_TIME_OF_DAY",
                    key="preferred_time_of_day",
                    value="MORNING",
                )
                contexts = application.state.context_application_service
                first = await contexts.build_snapshot(
                    application.state.local_user,
                    ContextBuildCommand(
                        agent_type=AgentType.PLAN_GENERATION,
                        current_task={"request_type": "redis-cache-test"},
                    ),
                    scope_id="redis-cache-first",
                )
                key = manager.build_key("memory", "active", "v1", str(user_id))
                client_redis = manager.get_client()
                unrelated_key = f"fitweek:test:sentinel:{uuid4().hex}:unrelated"
                await client_redis.set(unrelated_key, "present", ex=60)
                populated = await client_redis.get(key)
                assert populated is not None
                invalid_schema = json.loads(populated)
                invalid_schema["schema_version"] = 99
                await client_redis.set(key, json.dumps(invalid_schema), ex=60)
                schema_rebuilt = await contexts.build_snapshot(
                    application.state.local_user,
                    ContextBuildCommand(
                        agent_type=AgentType.PLAN_GENERATION,
                        current_task={"request_type": "redis-cache-test"},
                    ),
                    scope_id="redis-cache-schema",
                )
                assert {
                    item.id for item in schema_rebuilt.reference.memory_versions
                } == {
                    first_memory,
                    second_memory,
                }
                await client_redis.set(key, "{not-json", ex=60)
                rebuilt = await contexts.build_snapshot(
                    application.state.local_user,
                    ContextBuildCommand(
                        agent_type=AgentType.PLAN_GENERATION,
                        current_task={"request_type": "redis-cache-test"},
                    ),
                    scope_id="redis-cache-corrupt",
                )
                assert {item.id for item in rebuilt.reference.memory_versions} == {
                    first_memory,
                    second_memory,
                }
                repaired_payload = json.loads(await client_redis.get(key))
                assert repaired_payload["schema_version"] == 1
                assert isinstance(repaired_payload["items"], list)
                reversed_payload = dict(repaired_payload)
                reversed_payload["items"] = list(reversed(repaired_payload["items"]))
                await client_redis.set(key, json.dumps(reversed_payload), ex=60)
                reordered = await contexts.build_snapshot(
                    application.state.local_user,
                    ContextBuildCommand(
                        agent_type=AgentType.PLAN_GENERATION,
                        current_task={"request_type": "redis-cache-test"},
                    ),
                    scope_id="redis-cache-reordered",
                )
                assert (
                    reordered.reference.context_fingerprint
                    == rebuilt.reference.context_fingerprint
                )
                assert (
                    first.reference.context_fingerprint
                    == rebuilt.reference.context_fingerprint
                )
                assert await client_redis.get(unrelated_key) == "present"
    finally:
        if key is not None:
            await manager.get_client().delete(key)
        if unrelated_key is not None:
            await manager.get_client().delete(unrelated_key)
        await manager.close()
        await _cleanup(mysql_test_database, user_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_memory_and_context_degrade_when_redis_is_unreachable(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        app_env="test",
        persistence_backend=PersistenceBackend.MYSQL,
        database_url=SecretStr(mysql_test_url),
        redis_enabled=True,
        redis_url=SecretStr("redis://127.0.0.1:1/15"),
        redis_connect_timeout_seconds=0.1,
        redis_socket_timeout_seconds=0.1,
        redis_key_prefix=f"fitweek:test:{uuid4().hex}",
        single_user_email=f"redis-unavailable-{uuid4().hex}@fitweek.test",
        _env_file=None,
    )
    manager = RedisManager(settings)
    monkeypatch.setattr(main_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "get_redis_manager", lambda: manager)
    monkeypatch.setattr(health_module, "get_database", lambda: mysql_test_database)
    monkeypatch.setattr(health_module, "get_redis_manager", lambda: manager)
    application = main_module.create_application()
    application.dependency_overrides[get_settings] = lambda: settings
    user_id: UUID | None = None
    try:
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                user_id = UUID((await client.get("/api/v1/users/me")).json()["id"])
                memory_id = await _accept_memory(
                    client,
                    memory_type="PREFERRED_LOCATION",
                    key="preferred_location",
                    value="HOME",
                )
                listed = await client.get("/api/v1/memories")
                assert listed.status_code == 200
                assert [item["id"] for item in listed.json()] == [str(memory_id)]
                snapshot = (
                    await application.state.context_application_service.build_snapshot(
                        application.state.local_user,
                        ContextBuildCommand(
                            agent_type=AgentType.PLAN_GENERATION,
                            current_task={"request_type": "redis-unavailable"},
                        ),
                        scope_id="redis-unavailable-context",
                    )
                )
                assert [item.id for item in snapshot.reference.memory_versions] == [
                    memory_id
                ]
                assert (await client.get("/health/live")).status_code == 200
                ready = await client.get("/health/ready")
                assert ready.status_code == 200
                assert ready.json()["status"] == "degraded"
                assert ready.json()["checks"]["redis"] == "unavailable"
                assert "redis://" not in ready.text
    finally:
        application.dependency_overrides.clear()
        await manager.close()
        await _cleanup(mysql_test_database, user_id)
