"""MySQL adapter for user accounts used by local single-user mode."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.users.models import UserAccount, UserStatus
from app.persistence.mysql.models import UserAccountModel


class MySQLUserAccountRepository:
    """Persist accounts by email without relying on a fixed database primary key."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_by_email(self, email: str) -> UserAccount | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(UserAccountModel).where(
                    UserAccountModel.email == email.casefold()
                )
            )
            return self._to_domain(row) if row is not None else None

    async def save(self, user: UserAccount) -> UserAccount:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(UserAccountModel).where(
                        UserAccountModel.email == user.email.casefold()
                    )
                )
                if row is None:
                    row = UserAccountModel(
                        id=str(user.id),
                        email=user.email.casefold(),
                        display_name=user.display_name,
                        timezone=user.timezone,
                        status=user.status.value,
                        created_at=self._db_time(user.created_at),
                        updated_at=self._db_time(user.updated_at),
                        version=user.version,
                    )
                    session.add(row)
                else:
                    row.display_name = user.display_name
                    row.timezone = user.timezone
                    row.status = user.status.value
                    row.updated_at = self._db_time(user.updated_at)
                    row.version = user.version
            return self._to_domain(row)

    @staticmethod
    def _to_domain(row: UserAccountModel) -> UserAccount:
        from uuid import UUID

        return UserAccount(
            id=UUID(row.id),
            email=row.email,
            display_name=row.display_name,
            timezone=row.timezone,
            status=UserStatus(row.status),
            created_at=MySQLUserAccountRepository._utc(row.created_at),
            updated_at=MySQLUserAccountRepository._utc(row.updated_at),
            version=row.version,
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)
