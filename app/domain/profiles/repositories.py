"""Persistence-independent profile repository contract."""

from typing import Protocol
from uuid import UUID

from app.domain.profiles.models import FitnessProfile, UserConstraint


class ProfileRepository(Protocol):
    async def get_by_user_id(self, user_id: UUID) -> FitnessProfile | None: ...

    async def save(self, profile: FitnessProfile) -> FitnessProfile: ...

    async def list_constraints(self, profile_id: UUID) -> list[UserConstraint]: ...

    async def add_constraint(self, constraint: UserConstraint) -> UserConstraint: ...

    async def delete_constraint(
        self, profile_id: UUID, constraint_id: UUID
    ) -> bool: ...
