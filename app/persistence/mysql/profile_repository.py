"""MySQL adapter for fitness profiles and their structured constraints."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
    FitnessProfile,
    UserConstraint,
)
from app.persistence.mysql.models import FitnessProfileModel, UserConstraintModel


class MySQLProfileRepository:
    """Persist profile aggregates with explicit optimistic-version checks."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_by_user_id(self, user_id: UUID) -> FitnessProfile | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(FitnessProfileModel).where(
                    FitnessProfileModel.user_id == str(user_id)
                )
            )
            return None if row is None else self._profile_from_row(row)

    async def save(self, profile: FitnessProfile) -> FitnessProfile:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.get(FitnessProfileModel, str(profile.id))
                owner = await session.scalar(
                    select(FitnessProfileModel).where(
                        FitnessProfileModel.user_id == str(profile.user_id)
                    )
                )
                if row is None:
                    if profile.version != 1:
                        raise RepositoryConflictError(
                            "fitness_profile",
                            profile.id,
                            expected_version=1,
                            actual_version=profile.version,
                        )
                    if owner is not None:
                        raise RepositoryUniqueError(
                            "fitness_profile.user_id", profile.user_id
                        )
                    row = FitnessProfileModel(
                        id=str(profile.id),
                        user_id=str(profile.user_id),
                        experience_level=profile.experience_level.value,
                        primary_goal=profile.primary_goal.value,
                        weekly_frequency=profile.weekly_frequency,
                        max_session_minutes=profile.max_session_minutes,
                        scope_confirmed=profile.scope_confirmed,
                        created_at=self._db_time(profile.created_at),
                        updated_at=self._db_time(profile.updated_at),
                        version=profile.version,
                    )
                    session.add(row)
                else:
                    if row.user_id != str(profile.user_id):
                        raise RepositoryConflictError(
                            "fitness_profile.user_id",
                            profile.id,
                            expected_version=row.version + 1,
                            actual_version=profile.version,
                        )
                    if row.version + 1 != profile.version:
                        raise RepositoryConflictError(
                            "fitness_profile",
                            profile.id,
                            expected_version=row.version + 1,
                            actual_version=profile.version,
                        )
                    if owner is not None and owner.id != row.id:
                        raise RepositoryUniqueError(
                            "fitness_profile.user_id", profile.user_id
                        )
                    row.experience_level = profile.experience_level.value
                    row.primary_goal = profile.primary_goal.value
                    row.weekly_frequency = profile.weekly_frequency
                    row.max_session_minutes = profile.max_session_minutes
                    row.scope_confirmed = profile.scope_confirmed
                    row.updated_at = self._db_time(profile.updated_at)
                    row.version = profile.version
            return self._profile_from_row(row)

    async def list_constraints(self, profile_id: UUID) -> list[UserConstraint]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(UserConstraintModel)
                    .where(UserConstraintModel.profile_id == str(profile_id))
                    .order_by(
                        UserConstraintModel.priority.desc(),
                        UserConstraintModel.created_at,
                        UserConstraintModel.id,
                    )
                )
            ).all()
            return [self._constraint_from_row(row) for row in rows]

    async def add_constraint(self, constraint: UserConstraint) -> UserConstraint:
        async with self._sessions() as session:
            async with session.begin():
                existing = await session.get(UserConstraintModel, str(constraint.id))
                if existing is not None:
                    raise RepositoryUniqueError("user_constraint.id", constraint.id)
                if constraint.version != 1:
                    raise RepositoryConflictError(
                        "user_constraint",
                        constraint.id,
                        expected_version=1,
                        actual_version=constraint.version,
                    )
                row = UserConstraintModel(
                    id=str(constraint.id),
                    profile_id=str(constraint.profile_id),
                    constraint_type=constraint.constraint_type.value,
                    constraint_value=constraint.constraint_value,
                    priority=constraint.priority,
                    is_hard=constraint.is_hard,
                    source=constraint.source.value,
                    valid_until=(
                        None
                        if constraint.valid_until is None
                        else self._db_time(constraint.valid_until)
                    ),
                    created_at=self._db_time(constraint.created_at),
                    version=constraint.version,
                )
                session.add(row)
            return self._constraint_from_row(row)

    async def delete_constraint(self, profile_id: UUID, constraint_id: UUID) -> bool:
        async with self._sessions() as session:
            async with session.begin():
                result = await session.execute(
                    delete(UserConstraintModel).where(
                        UserConstraintModel.id == str(constraint_id),
                        UserConstraintModel.profile_id == str(profile_id),
                    )
                )
            return bool(getattr(result, "rowcount", 0))

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)

    @classmethod
    def _profile_from_row(cls, row: FitnessProfileModel) -> FitnessProfile:
        return FitnessProfile(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            experience_level=ExperienceLevel(row.experience_level),
            weekly_frequency=row.weekly_frequency,
            max_session_minutes=row.max_session_minutes,
            primary_goal=FitnessGoal(row.primary_goal),
            scope_confirmed=row.scope_confirmed,
            created_at=cls._utc(row.created_at),
            updated_at=cls._utc(row.updated_at),
            version=row.version,
        )

    @classmethod
    def _constraint_from_row(cls, row: UserConstraintModel) -> UserConstraint:
        return UserConstraint(
            id=UUID(row.id),
            profile_id=UUID(row.profile_id),
            constraint_type=ConstraintType(row.constraint_type),
            constraint_value=row.constraint_value,
            priority=row.priority,
            is_hard=row.is_hard,
            source=ConstraintSource(row.source),
            valid_until=None if row.valid_until is None else cls._utc(row.valid_until),
            created_at=cls._utc(row.created_at),
            version=row.version,
        )
