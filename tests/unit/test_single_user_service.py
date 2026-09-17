"""Idempotent local-user seeding contracts."""

from dataclasses import replace

import pytest

from app.application.local_user import ensure_local_user
from app.config import Settings
from app.domain.users.models import UserAccount


class InMemoryUserPort:
    def __init__(self) -> None:
        self.by_email: dict[str, UserAccount] = {}

    async def get_by_email(self, email: str) -> UserAccount | None:
        return self.by_email.get(email)

    async def save(self, user: UserAccount) -> UserAccount:
        existing = self.by_email.get(user.email)
        if existing is not None:
            user = replace(user, id=existing.id, created_at=existing.created_at)
        self.by_email[user.email] = user
        return user


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_refreshes_configurable_display_name() -> None:
    users = InMemoryUserPort()
    first = await ensure_local_user(
        users,
        Settings(
            single_user_email="member@fitweek.local",
            single_user_display_name="初始名称",
            _env_file=None,
        ),
    )
    second = await ensure_local_user(
        users,
        Settings(
            single_user_email="member@fitweek.local",
            single_user_display_name="更新名称",
            _env_file=None,
        ),
    )

    assert len(users.by_email) == 1
    assert second.id == first.id
    assert second.display_name == "更新名称"
