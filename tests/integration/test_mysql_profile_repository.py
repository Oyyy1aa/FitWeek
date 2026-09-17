"""Real MySQL contract for profile and constraint persistence."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.domain.profiles.models import ConstraintType
from app.persistence.database import Database
from app.persistence.mysql.models import (
    FitnessProfileModel,
    UserAccountModel,
    UserConstraintModel,
)
from app.persistence.mysql.profile_repository import MySQLProfileRepository
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.factories import make_constraint, make_profile, make_user


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_profile_and_constraints_preserve_user_and_version_boundaries(
    mysql_test_database: Database,
) -> None:
    user = replace(make_user(), email=f"profile-{uuid4().hex}@fitweek.test")
    profile = make_profile(user_id=user.id)
    repository = MySQLProfileRepository(mysql_test_database.session_factory)
    users = MySQLUserAccountRepository(mysql_test_database.session_factory)
    higher = make_constraint(
        profile.id,
        constraint_type=ConstraintType.AVAILABLE_EQUIPMENT,
        value="band",
        priority=100,
    )
    lower = make_constraint(
        profile.id,
        constraint_type=ConstraintType.EXCLUDED_FEATURE,
        value="jumping",
        priority=10,
    )
    try:
        await users.save(user)
        assert await repository.save(profile) == profile
        assert await repository.get_by_user_id(user.id) == profile

        updated = replace(profile, weekly_frequency=3, version=2)
        assert await repository.save(updated) == updated
        assert await repository.add_constraint(lower) == lower
        assert await repository.add_constraint(higher) == higher
        assert await repository.list_constraints(profile.id) == [higher, lower]
        assert not await repository.delete_constraint(uuid4(), higher.id)
        assert await repository.delete_constraint(profile.id, higher.id)
        assert await repository.list_constraints(profile.id) == [lower]
    finally:
        async with mysql_test_database.session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(UserConstraintModel).where(
                        UserConstraintModel.profile_id == str(profile.id)
                    )
                )
                await session.execute(
                    delete(FitnessProfileModel).where(
                        FitnessProfileModel.id == str(profile.id)
                    )
                )
                await session.execute(
                    delete(UserAccountModel).where(UserAccountModel.id == str(user.id))
                )
