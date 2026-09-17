"""MySQL persistence for immutable, idempotent workout check-ins."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.domain.checkins.models import CheckInStatus, WorkoutCheckIn
from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.persistence.mysql.models import SessionCheckinModel


class MySQLCheckInRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, check_in_id: UUID) -> WorkoutCheckIn | None:
        async with self._sessions() as session:
            row = await session.get(SessionCheckinModel, str(check_in_id))
            return None if row is None else self._from_row(row)

    async def get_by_client_event_id(
        self, user_id: UUID, client_event_id: str
    ) -> WorkoutCheckIn | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(SessionCheckinModel).where(
                    SessionCheckinModel.user_id == str(user_id),
                    SessionCheckinModel.client_event_id == client_event_id,
                )
            )
            return None if row is None else self._from_row(row)

    async def get_by_session_id(self, session_id: UUID) -> WorkoutCheckIn | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(SessionCheckinModel).where(
                    SessionCheckinModel.session_id == str(session_id)
                )
            )
            return None if row is None else self._from_row(row)

    async def save(self, check_in: WorkoutCheckIn) -> WorkoutCheckIn:
        async with self._sessions() as session:
            try:
                async with session.begin():
                    row = await session.scalar(
                        select(SessionCheckinModel).where(
                            SessionCheckinModel.user_id == str(check_in.user_id),
                            SessionCheckinModel.client_event_id
                            == check_in.client_event_id,
                        )
                    )
                    if row is not None:
                        existing = self._from_row(row)
                        if existing.same_event_payload(check_in):
                            return existing
                        raise RepositoryUniqueError(
                            "check_in.user_client_event", check_in.client_event_id
                        )
                    if check_in.version != 1:
                        raise RepositoryConflictError(
                            "session_checkin",
                            check_in.id,
                            expected_version=1,
                            actual_version=check_in.version,
                        )
                    session.add(
                        SessionCheckinModel(
                            id=str(check_in.id),
                            client_event_id=check_in.client_event_id,
                            user_id=str(check_in.user_id),
                            plan_id=str(check_in.plan_id),
                            plan_revision=check_in.plan_revision,
                            session_id=str(check_in.session_id),
                            status=check_in.status.value,
                            actual_minutes=check_in.actual_minutes,
                            perceived_effort=check_in.perceived_effort,
                            note=check_in.note,
                            occurred_at=self._db_time(check_in.occurred_at),
                            created_at=self._db_time(check_in.created_at),
                            updated_at=self._db_time(check_in.updated_at),
                            version=check_in.version,
                        )
                    )
                return check_in
            except IntegrityError as exc:
                retrieved = await self.get_by_client_event_id(
                    check_in.user_id, check_in.client_event_id
                )
                if retrieved is not None:
                    if retrieved.same_event_payload(check_in):
                        return retrieved
                raise RepositoryUniqueError(
                    "session_checkin.constraint", check_in.id
                ) from exc

    async def list_by_plan(
        self, plan_id: UUID, plan_revision: int
    ) -> list[WorkoutCheckIn]:
        return await self._list(
            SessionCheckinModel.plan_id == str(plan_id),
            SessionCheckinModel.plan_revision == plan_revision,
        )

    async def list_by_series(self, plan_id: UUID) -> list[WorkoutCheckIn]:
        return await self._list(SessionCheckinModel.plan_id == str(plan_id))

    async def list_by_user(self, user_id: UUID) -> list[WorkoutCheckIn]:
        return await self._list(SessionCheckinModel.user_id == str(user_id))

    async def _list(self, *criteria: ColumnElement[bool]) -> list[WorkoutCheckIn]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(SessionCheckinModel)
                    .where(*criteria)
                    .order_by(SessionCheckinModel.occurred_at, SessionCheckinModel.id)
                )
            ).all()
            return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: SessionCheckinModel) -> WorkoutCheckIn:
        def utc(value: datetime) -> datetime:
            return (
                value.replace(tzinfo=UTC)
                if value.tzinfo is None
                else value.astimezone(UTC)
            )

        return WorkoutCheckIn(
            id=UUID(row.id),
            client_event_id=row.client_event_id,
            user_id=UUID(row.user_id),
            plan_id=UUID(row.plan_id),
            plan_revision=row.plan_revision,
            session_id=UUID(row.session_id),
            status=CheckInStatus(row.status),
            actual_minutes=row.actual_minutes,
            perceived_effort=row.perceived_effort,
            note=row.note,
            occurred_at=utc(row.occurred_at),
            created_at=utc(row.created_at),
            updated_at=utc(row.updated_at),
            version=row.version,
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)
