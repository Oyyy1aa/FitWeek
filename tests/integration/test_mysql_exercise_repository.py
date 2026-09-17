"""MySQL catalog seed and filtering contract."""

import pytest

from app.domain.common import LocationType
from app.domain.exercises.catalog_seed import CATALOG_SEED
from app.persistence.database import Database
from app.persistence.mysql.exercise_repository import MySQLExerciseRepository


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_exercise_catalog_seed_is_repeatable_and_queryable(
    mysql_test_database: Database,
) -> None:
    repository = MySQLExerciseRepository(mysql_test_database.session_factory)

    await repository.seed(CATALOG_SEED)
    await repository.seed(CATALOG_SEED)

    assert await repository.get("bodyweight_squat") == next(
        item for item in CATALOG_SEED if item.id == "bodyweight_squat"
    )
    assert all(
        LocationType.HOME in item.location_types
        for item in await repository.list_active(location=LocationType.HOME)
    )
