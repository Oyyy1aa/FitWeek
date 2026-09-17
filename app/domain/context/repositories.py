"""Repository-neutral Context Snapshot persistence boundary."""

from typing import Protocol
from uuid import UUID

from app.domain.context.enums import AgentType
from app.domain.context.models import FrozenContextSnapshot


class ContextSnapshotRepository(Protocol):
    async def get(
        self, user_id: UUID, snapshot_id: UUID
    ) -> FrozenContextSnapshot | None: ...

    async def get_by_scope(
        self,
        user_id: UUID,
        agent_type: AgentType,
        scope_id: str,
    ) -> FrozenContextSnapshot | None: ...

    async def save(
        self,
        *,
        scope_id: str,
        snapshot: FrozenContextSnapshot,
    ) -> FrozenContextSnapshot: ...

    async def reset(self) -> None: ...
