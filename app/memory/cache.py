"""Optional, lossy cache boundary for safe ACTIVE Memory views."""

from typing import Protocol
from uuid import UUID

from app.domain.memory.models import UserMemory


class MemoryActiveCache(Protocol):
    async def get_active(self, user_id: UUID) -> tuple[UserMemory, ...] | None: ...

    async def set_active(
        self, user_id: UUID, memories: tuple[UserMemory, ...]
    ) -> None: ...

    async def invalidate_active(self, user_id: UUID) -> None: ...
