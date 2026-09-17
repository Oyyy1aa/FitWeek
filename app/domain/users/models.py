"""Minimal development-user model; authentication is intentionally out of scope."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.common import (
    DomainValidationError,
    require_non_blank,
    require_utc_datetime,
    require_version,
)


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True, kw_only=True)
class UserAccount:
    id: UUID
    email: str
    timezone: str
    status: UserStatus
    created_at: datetime
    updated_at: datetime
    version: int
    display_name: str = "FitWeek 用户"

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise DomainValidationError("id must be a UUID.")
        if not isinstance(self.status, UserStatus):
            raise DomainValidationError("status must be a UserStatus.")
        require_non_blank(self.email, "email")
        if (
            "@" not in self.email
            or self.email.startswith("@")
            or self.email.endswith("@")
        ):
            raise DomainValidationError("email must have a valid address shape.")
        require_non_blank(self.timezone, "timezone")
        require_non_blank(self.display_name, "display_name")
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise DomainValidationError("updated_at cannot precede created_at.")
        require_version(self.version)
