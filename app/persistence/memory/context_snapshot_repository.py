"""Shared-store process-local Context Snapshot adapter."""

from copy import deepcopy
from uuid import UUID

from app.domain.context.enums import AgentType
from app.domain.context.models import FrozenContextSnapshot
from app.domain.memory.errors import ContextSnapshotVersionMismatchError
from app.persistence.memory.store import InMemoryStore


class InMemoryContextSnapshotRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get(
        self, user_id: UUID, snapshot_id: UUID
    ) -> FrozenContextSnapshot | None:
        async with self._store.lock:
            snapshot = self._store._context_snapshots.get(snapshot_id)
            if snapshot is None or snapshot.reference.user_id != user_id:
                return None
            return deepcopy(snapshot)

    async def get_by_scope(
        self,
        user_id: UUID,
        agent_type: AgentType,
        scope_id: str,
    ) -> FrozenContextSnapshot | None:
        async with self._store.lock:
            snapshot_id = self._store._context_snapshot_by_scope.get(
                (user_id, agent_type, scope_id)
            )
            if snapshot_id is None:
                return None
            return deepcopy(self._store._context_snapshots[snapshot_id])

    async def save(
        self,
        *,
        scope_id: str,
        snapshot: FrozenContextSnapshot,
    ) -> FrozenContextSnapshot:
        key = (
            snapshot.reference.user_id,
            snapshot.reference.agent_type,
            scope_id,
        )
        async with self._store.lock:
            existing_id = self._store._context_snapshot_by_scope.get(key)
            if existing_id is not None:
                existing = self._store._context_snapshots[existing_id]
                if existing.reference.context_fingerprint != (
                    snapshot.reference.context_fingerprint
                ):
                    raise ContextSnapshotVersionMismatchError(
                        "The Context Snapshot scope is already frozen."
                    )
                return deepcopy(existing)
            by_id = self._store._context_snapshots.get(snapshot.reference.id)
            if by_id is not None and by_id != snapshot:
                raise ContextSnapshotVersionMismatchError(
                    "Context Snapshot ID already contains another payload."
                )
            self._store._context_snapshots[snapshot.reference.id] = deepcopy(snapshot)
            self._store._context_snapshot_by_scope[key] = snapshot.reference.id
            return deepcopy(snapshot)

    async def reset(self) -> None:
        async with self._store.lock:
            self._store._context_snapshots.clear()
            self._store._context_snapshot_by_scope.clear()
