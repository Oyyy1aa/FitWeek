"""Persistence-neutral local user account boundary."""

from typing import Protocol

from app.domain.users.models import UserAccount


class UserAccountRepository(Protocol):
    """Store user accounts independently from the authentication mechanism."""

    async def get_by_email(self, email: str) -> UserAccount | None: ...

    async def save(self, user: UserAccount) -> UserAccount: ...
