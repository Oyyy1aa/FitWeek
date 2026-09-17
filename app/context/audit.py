"""User-isolated Context Audit query service."""

from uuid import UUID

from app.domain.context.models import ContextBuildAudit
from app.domain.memory.errors import MemoryNotFoundError
from app.domain.memory.repositories import MemoryRepository


class ContextAuditService:
    def __init__(self, repository: MemoryRepository) -> None:
        self._repository = repository

    async def get(self, user_id: UUID, audit_id: UUID) -> ContextBuildAudit:
        audit = await self._repository.get_context_audit(user_id, audit_id)
        if audit is None:
            raise MemoryNotFoundError("Context Audit was not found.")
        return audit
