"""ICS export persistence port."""

from typing import Protocol
from uuid import UUID

from app.domain.ics.models import IcsExportRecord


class IcsExportRepository(Protocol):
    async def get(self, user_id: UUID, export_id: UUID) -> IcsExportRecord | None: ...
    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> IcsExportRecord | None: ...
    async def save(self, record: IcsExportRecord) -> IcsExportRecord: ...
