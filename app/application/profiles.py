"""Profile and structured-constraint use cases."""

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4

from app.application.errors import (
    BusinessRuleViolation,
    ConflictError,
    ResourceNotFound,
)
from app.domain.common import RepositoryError, utc_now
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
    FitnessProfile,
    UserConstraint,
)
from app.domain.profiles.normalization import normalize_constraint_value
from app.domain.profiles.repositories import ProfileRepository
from app.domain.users.models import UserAccount


@dataclass(frozen=True, slots=True, kw_only=True)
class UpsertProfileCommand:
    experience_level: ExperienceLevel
    weekly_frequency: int
    max_session_minutes: int
    primary_goal: FitnessGoal
    scope_confirmed: bool
    expected_version: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AddConstraintCommand:
    constraint_type: ConstraintType
    constraint_value: str
    priority: int
    is_hard: bool
    source: ConstraintSource
    valid_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProfileView:
    profile: FitnessProfile
    constraints: tuple[UserConstraint, ...]


class ProfileService:
    """Coordinate immutable profile snapshots through a repository protocol."""

    def __init__(self, repository: ProfileRepository) -> None:
        self._repository = repository

    async def get_profile(self, user: UserAccount) -> ProfileView:
        profile = await self._repository.get_by_user_id(user.id)
        if profile is None:
            raise ResourceNotFound("Fitness profile was not found.")
        constraints = await self._repository.list_constraints(profile.id)
        return ProfileView(profile=profile, constraints=tuple(constraints))

    async def upsert_profile(
        self,
        user: UserAccount,
        command: UpsertProfileCommand,
    ) -> ProfileView:
        current = await self._repository.get_by_user_id(user.id)
        if current is None:
            if command.expected_version not in {None, 0}:
                raise ConflictError("A new profile expects version 0.")
            now = utc_now()
            candidate = FitnessProfile(
                id=uuid4(),
                user_id=user.id,
                experience_level=command.experience_level,
                weekly_frequency=command.weekly_frequency,
                max_session_minutes=command.max_session_minutes,
                primary_goal=command.primary_goal,
                scope_confirmed=command.scope_confirmed,
                created_at=now,
                updated_at=now,
                version=1,
            )
        else:
            unchanged = (
                current.experience_level is command.experience_level
                and current.weekly_frequency == command.weekly_frequency
                and current.max_session_minutes == command.max_session_minutes
                and current.primary_goal is command.primary_goal
                and current.scope_confirmed is command.scope_confirmed
            )
            if unchanged:
                constraints = await self._repository.list_constraints(current.id)
                return ProfileView(current, tuple(constraints))
            if command.expected_version != current.version:
                raise ConflictError(
                    "Profile expected_version does not match the current version."
                )
            candidate = replace(
                current,
                experience_level=command.experience_level,
                weekly_frequency=command.weekly_frequency,
                max_session_minutes=command.max_session_minutes,
                primary_goal=command.primary_goal,
                scope_confirmed=command.scope_confirmed,
                updated_at=utc_now(),
                version=current.version + 1,
            )
        try:
            saved = await self._repository.save(candidate)
        except RepositoryError as exc:
            raise ConflictError(
                "Profile could not be saved due to a conflict."
            ) from exc
        constraints = await self._repository.list_constraints(saved.id)
        return ProfileView(saved, tuple(constraints))

    async def add_constraint(
        self,
        user: UserAccount,
        command: AddConstraintCommand,
    ) -> UserConstraint:
        profile = (await self.get_profile(user)).profile
        try:
            normalized = normalize_constraint_value(
                command.constraint_type,
                command.constraint_value,
            )
        except ValueError as exc:
            raise BusinessRuleViolation(
                "Constraint value is not valid for its structured type.",
                code="INVALID_CONSTRAINT_VALUE",
            ) from exc
        constraint = UserConstraint(
            id=uuid4(),
            profile_id=profile.id,
            constraint_type=command.constraint_type,
            constraint_value=normalized,
            priority=command.priority,
            is_hard=command.is_hard,
            source=command.source,
            valid_until=command.valid_until,
            created_at=utc_now(),
            version=1,
        )
        try:
            return await self._repository.add_constraint(constraint)
        except RepositoryError as exc:
            raise ConflictError(
                "Constraint could not be saved due to a conflict."
            ) from exc

    async def delete_constraint(
        self,
        user: UserAccount,
        constraint_id: UUID,
    ) -> None:
        profile = (await self.get_profile(user)).profile
        if not await self._repository.delete_constraint(profile.id, constraint_id):
            raise ResourceNotFound("Constraint was not found.")
