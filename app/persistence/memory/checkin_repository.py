"""Process-local check-in repository with atomic idempotency constraints."""

from copy import deepcopy
from uuid import UUID

from app.domain.checkins.models import WorkoutCheckIn
from app.persistence.memory.store import InMemoryStore, RepositoryUniqueError


class InMemoryCheckInRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get(self, check_in_id: UUID) -> WorkoutCheckIn | None:
        async with self._store.lock:
            return deepcopy(self._store._check_ins.get(check_in_id))

    async def get_by_client_event_id(
        self, user_id: UUID, client_event_id: str
    ) -> WorkoutCheckIn | None:
        async with self._store.lock:
            check_in_id = self._store._check_in_id_by_user_event.get(
                (user_id, client_event_id)
            )
            return (
                None
                if check_in_id is None
                else deepcopy(self._store._check_ins[check_in_id])
            )

    async def get_by_session_id(self, session_id: UUID) -> WorkoutCheckIn | None:
        async with self._store.lock:
            check_in_id = self._store._check_in_id_by_session.get(session_id)
            return (
                None
                if check_in_id is None
                else deepcopy(self._store._check_ins[check_in_id])
            )

    async def save(self, check_in: WorkoutCheckIn) -> WorkoutCheckIn:
        snapshot = deepcopy(check_in)
        event_key = (snapshot.user_id, snapshot.client_event_id)
        async with self._store.lock:
            existing_event_id = self._store._check_in_id_by_user_event.get(event_key)
            if existing_event_id is not None:
                existing = self._store._check_ins[existing_event_id]
                if existing.same_event_payload(snapshot):
                    return deepcopy(existing)
                raise RepositoryUniqueError("check_in.user_client_event", event_key)
            existing_session_id = self._store._check_in_id_by_session.get(
                snapshot.session_id
            )
            if existing_session_id is not None:
                raise RepositoryUniqueError("check_in.session_id", snapshot.session_id)
            self._store.require_first_version(
                "workout_check_in", snapshot.id, snapshot.version
            )
            self._store._check_ins[snapshot.id] = snapshot
            self._store._check_in_id_by_user_event[event_key] = snapshot.id
            self._store._check_in_id_by_session[snapshot.session_id] = snapshot.id
            return deepcopy(snapshot)

    async def list_by_plan(
        self, plan_id: UUID, plan_revision: int
    ) -> list[WorkoutCheckIn]:
        async with self._store.lock:
            matches = [
                check_in
                for check_in in self._store._check_ins.values()
                if check_in.plan_id == plan_id
                and check_in.plan_revision == plan_revision
            ]
            matches.sort(key=lambda item: (item.occurred_at, item.id))
            return deepcopy(matches)

    async def list_by_series(self, plan_id: UUID) -> list[WorkoutCheckIn]:
        async with self._store.lock:
            matches = [
                check_in
                for check_in in self._store._check_ins.values()
                if check_in.plan_id == plan_id
            ]
            matches.sort(key=lambda item: (item.occurred_at, item.id))
            return deepcopy(matches)

    async def list_by_user(self, user_id: UUID) -> list[WorkoutCheckIn]:
        async with self._store.lock:
            matches = [
                check_in
                for check_in in self._store._check_ins.values()
                if check_in.user_id == user_id
            ]
            matches.sort(key=lambda item: (item.occurred_at, item.id))
            return deepcopy(matches)

    async def clear(self) -> None:
        async with self._store.lock:
            self._store._check_ins.clear()
            self._store._check_in_id_by_user_event.clear()
            self._store._check_in_id_by_session.clear()
