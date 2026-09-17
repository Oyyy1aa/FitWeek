"""In-memory profile and structured-constraint repository."""

from copy import deepcopy
from uuid import UUID

from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.persistence.memory.store import (
    InMemoryStore,
    RepositoryConflictError,
    RepositoryUniqueError,
)


class InMemoryProfileRepository:
    """Persist immutable profile snapshots behind the repository protocol."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get_by_user_id(self, user_id: UUID) -> FitnessProfile | None:
        async with self._store.lock:
            profile_id = self._store._profile_id_by_user.get(user_id)
            if profile_id is None:
                return None
            return deepcopy(self._store._profiles[profile_id])

    async def save(self, profile: FitnessProfile) -> FitnessProfile:
        snapshot = deepcopy(profile)
        async with self._store.lock:
            existing = self._store._profiles.get(snapshot.id)
            owner_profile_id = self._store._profile_id_by_user.get(snapshot.user_id)
            if existing is None:
                self._store.require_first_version(
                    "fitness_profile", snapshot.id, snapshot.version
                )
                if owner_profile_id is not None:
                    raise RepositoryUniqueError(
                        "fitness_profile.user_id", snapshot.user_id
                    )
            else:
                if existing.user_id != snapshot.user_id:
                    raise RepositoryConflictError(
                        "fitness_profile.user_id",
                        snapshot.id,
                        expected_version=existing.version + 1,
                        actual_version=snapshot.version,
                    )
                self._store.require_next_version(
                    "fitness_profile",
                    snapshot.id,
                    current_version=existing.version,
                    incoming_version=snapshot.version,
                )
                if owner_profile_id not in {None, snapshot.id}:
                    raise RepositoryUniqueError(
                        "fitness_profile.user_id", snapshot.user_id
                    )

            self._store._profiles[snapshot.id] = snapshot
            self._store._profile_id_by_user[snapshot.user_id] = snapshot.id
            return deepcopy(snapshot)

    async def list_constraints(self, profile_id: UUID) -> list[UserConstraint]:
        async with self._store.lock:
            matches = [
                constraint
                for constraint in self._store._constraints.values()
                if constraint.profile_id == profile_id
            ]
            matches.sort(key=lambda item: (-item.priority, item.created_at, item.id))
            return deepcopy(matches)

    async def add_constraint(self, constraint: UserConstraint) -> UserConstraint:
        snapshot = deepcopy(constraint)
        async with self._store.lock:
            self._store.require_first_version(
                "user_constraint", snapshot.id, snapshot.version
            )
            if snapshot.id in self._store._constraints:
                raise RepositoryUniqueError("user_constraint.id", snapshot.id)
            self._store._constraints[snapshot.id] = snapshot
            return deepcopy(snapshot)

    async def delete_constraint(self, profile_id: UUID, constraint_id: UUID) -> bool:
        async with self._store.lock:
            existing = self._store._constraints.get(constraint_id)
            if existing is None or existing.profile_id != profile_id:
                return False
            del self._store._constraints[constraint_id]
            return True

    async def clear(self) -> None:
        """Clear profile state while leaving other repository data intact."""

        async with self._store.lock:
            self._store._profiles.clear()
            self._store._profile_id_by_user.clear()
            self._store._constraints.clear()
