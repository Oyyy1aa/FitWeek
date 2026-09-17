"""Immutable metadata and in-memory content for one ICS export."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.common import require_non_blank, require_utc_datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class IcsExportResult:
    id: UUID
    user_id: UUID
    client_request_id: str
    request_fingerprint: str
    root_plan_id: UUID
    revision: int
    plan_version: int
    policy_version: str
    content_sha256: str
    event_count: int
    byte_size: int
    filename: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "client_request_id",
            "request_fingerprint",
            "policy_version",
            "content_sha256",
            "filename",
        ):
            require_non_blank(getattr(self, name), name)
        require_utc_datetime(self.created_at, "created_at")
        if self.event_count < 0 or self.byte_size <= 0:
            raise ValueError("ICS counters are invalid.")


@dataclass(frozen=True, slots=True, kw_only=True)
class IcsExportRecord:
    result: IcsExportResult
    content: bytes
