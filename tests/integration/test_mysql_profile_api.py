"""Real MySQL composition coverage for the profile API boundary."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete

import app.main as main_module
from app.config import PersistenceBackend, Settings
from app.persistence.database import Database
from app.persistence.mysql.models import (
    FitnessProfileModel,
    UserAccountModel,
    UserConstraintModel,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_profile_api_persists_across_requests(
    mysql_test_database: Database,
    mysql_test_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MySQL lifespan wires profile reads/writes without an in-memory container."""

    email = f"profile-api-{uuid4().hex}@example.test"
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
    payload = {
        "experience_level": "BEGINNER",
        "weekly_frequency": 3,
        "max_session_minutes": 45,
        "primary_goal": "GENERAL_FITNESS",
        "scope_confirmed": True,
        "expected_version": 0,
    }
    user_id: str | None = None
    try:
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application),
                base_url="http://test",
            ) as client:
                user_response = await client.get("/api/v1/users/me")
                user_id = user_response.json()["id"]
                written = await client.put("/api/v1/profiles/me", json=payload)
                restored = await client.get("/api/v1/profiles/me")
        assert written.status_code == 200
        assert restored.status_code == 200
        assert restored.json()["version"] == 1
        assert restored.json()["weekly_frequency"] == 3
    finally:
        if user_id is not None:
            async with mysql_test_database.session_factory() as session:
                async with session.begin():
                    profile_ids = (
                        await session.scalars(
                            FitnessProfileModel.__table__.select()
                            .with_only_columns(FitnessProfileModel.id)
                            .where(FitnessProfileModel.user_id == user_id)
                        )
                    ).all()
                    if profile_ids:
                        await session.execute(
                            delete(UserConstraintModel).where(
                                UserConstraintModel.profile_id.in_(profile_ids)
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
