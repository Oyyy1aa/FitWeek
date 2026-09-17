"""Process-local ICS bytes and metadata repository."""

from copy import deepcopy
from uuid import UUID

from app.domain.common import RepositoryUniqueError
from app.domain.ics.models import IcsExportRecord
from app.persistence.memory.store import InMemoryStore


class InMemoryIcsExportRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get(self, user_id: UUID, export_id: UUID) -> IcsExportRecord | None:
        async with self._store.lock:
            record = self._store._ics_exports.get(export_id)
            return (
                deepcopy(record)
                if record and record.result.user_id == user_id
                else None
            )

    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> IcsExportRecord | None:
        async with self._store.lock:
            export_id = self._store._ics_export_id_by_request.get(
                (user_id, client_request_id)
            )
            return deepcopy(self._store._ics_exports[export_id]) if export_id else None

    async def save(self, record: IcsExportRecord) -> IcsExportRecord:
        key = (record.result.user_id, record.result.client_request_id)
        async with self._store.lock:
            existing_id = self._store._ics_export_id_by_request.get(key)
            if existing_id is not None:
                existing = self._store._ics_exports[existing_id]
                if (
                    existing.result.request_fingerprint
                    != record.result.request_fingerprint
                ):
                    raise RepositoryUniqueError("ics_export.user_request", key)
                return deepcopy(existing)
            self._store._ics_exports[record.result.id] = deepcopy(record)
            self._store._ics_export_id_by_request[key] = record.result.id
            return deepcopy(record)
