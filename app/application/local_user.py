"""Local single-user identity construction without authentication state."""

from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from app.config import Settings
from app.domain.common import utc_now
from app.domain.users.models import UserAccount, UserStatus
from app.domain.users.repositories import UserAccountRepository


def build_local_user(settings: Settings) -> UserAccount:
    """Build a deterministic local identity from its configured email address.

    The identifier is stable across process restarts but is not a database primary-key
    assumption. The persistence adapter resolves the account by email first.
    """

    now = utc_now()
    email = settings.single_user_email.casefold()
    return UserAccount(
        id=uuid5(NAMESPACE_URL, f"fitweek:single-user:{email}"),
        email=email,
        display_name=settings.single_user_display_name,
        timezone=settings.single_user_timezone,
        status=UserStatus.ACTIVE,
        created_at=now,
        updated_at=now,
        version=1,
    )


async def ensure_local_user(
    users: UserAccountRepository,
    settings: Settings,
) -> UserAccount:
    """Create the local account by email or update its mutable presentation fields."""

    candidate = build_local_user(settings)
    existing = await users.get_by_email(candidate.email)
    if existing is None:
        return await users.save(candidate)
    if (
        existing.display_name == candidate.display_name
        and existing.timezone == candidate.timezone
        and existing.status is UserStatus.ACTIVE
    ):
        return existing
    return await users.save(
        replace(
            existing,
            display_name=candidate.display_name,
            timezone=candidate.timezone,
            status=UserStatus.ACTIVE,
            updated_at=utc_now(),
            version=existing.version + 1,
        )
    )
