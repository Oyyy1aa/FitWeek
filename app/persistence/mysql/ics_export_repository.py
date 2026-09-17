"""MySQL storage for immutable ICS export bytes and metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.common import RepositoryUniqueError
from app.domain.ics.models import IcsExportRecord, IcsExportResult
from app.persistence.mysql.models import IcsExportModel


class MySQLIcsExportRepository:
    """Persist one immutable export per user idempotency request."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, user_id: UUID, export_id: UUID) -> IcsExportRecord | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(IcsExportModel).where(
                    IcsExportModel.id == str(export_id),
                    IcsExportModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._from_row(row)

    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> IcsExportRecord | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(IcsExportModel).where(
                    IcsExportModel.user_id == str(user_id),
                    IcsExportModel.client_request_id == client_request_id,
                )
            )
            return None if row is None else self._from_row(row)

    async def save(self, record: IcsExportRecord) -> IcsExportRecord:
        result = record.result
        request_key = (result.user_id, result.client_request_id)
        try:
            async with self._sessions() as session:
                async with session.begin():
                    existing = await session.scalar(
                        select(IcsExportModel)
                        .where(
                            IcsExportModel.user_id == str(result.user_id),
                            IcsExportModel.client_request_id
                            == result.client_request_id,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        return self._existing_or_conflict(existing, record, request_key)
                    collision = await session.get(IcsExportModel, str(result.id))
                    if collision is not None:
                        raise RepositoryUniqueError("ics_export.id", result.id)
                    session.add(self._to_row(record))
                    await session.flush()
                    return record
        except IntegrityError as exc:
            existing_record = await self.get_by_request(
                result.user_id, result.client_request_id
            )
            if existing_record is not None:
                if (
                    existing_record.result.request_fingerprint
                    == result.request_fingerprint
                ):
                    return existing_record
                raise RepositoryUniqueError(
                    "ics_export.user_request", request_key
                ) from exc
            if await self._get_by_id(result.id) is not None:
                raise RepositoryUniqueError("ics_export.id", result.id) from exc
            raise

    async def _get_by_id(self, export_id: UUID) -> IcsExportRecord | None:
        async with self._sessions() as session:
            row = await session.get(IcsExportModel, str(export_id))
            return None if row is None else self._from_row(row)

    @staticmethod
    def _existing_or_conflict(
        row: IcsExportModel,
        record: IcsExportRecord,
        request_key: tuple[UUID, str],
    ) -> IcsExportRecord:
        existing = MySQLIcsExportRepository._from_row(row)
        if existing.result.request_fingerprint != record.result.request_fingerprint:
            raise RepositoryUniqueError("ics_export.user_request", request_key)
        return existing

    @staticmethod
    def _to_row(record: IcsExportRecord) -> IcsExportModel:
        result = record.result
        return IcsExportModel(
            id=str(result.id),
            user_id=str(result.user_id),
            client_request_id=result.client_request_id,
            request_fingerprint=result.request_fingerprint,
            root_plan_id=str(result.root_plan_id),
            revision=result.revision,
            plan_version=result.plan_version,
            policy_version=result.policy_version,
            content_sha256=result.content_sha256,
            event_count=result.event_count,
            byte_size=result.byte_size,
            filename=result.filename,
            content=record.content,
            created_at=MySQLIcsExportRepository._db_time(result.created_at),
        )

    @staticmethod
    def _from_row(row: IcsExportModel) -> IcsExportRecord:
        return IcsExportRecord(
            result=IcsExportResult(
                id=UUID(row.id),
                user_id=UUID(row.user_id),
                client_request_id=row.client_request_id,
                request_fingerprint=row.request_fingerprint,
                root_plan_id=UUID(row.root_plan_id),
                revision=row.revision,
                plan_version=row.plan_version,
                policy_version=row.policy_version,
                content_sha256=row.content_sha256,
                event_count=row.event_count,
                byte_size=row.byte_size,
                filename=row.filename,
                created_at=MySQLIcsExportRepository._domain_time(row.created_at),
            ),
            content=bytes(row.content),
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)

    @staticmethod
    def _domain_time(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC)
