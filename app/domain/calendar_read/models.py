"""Privacy-minimal values crossing the read-only calendar boundary."""

from dataclasses import dataclass
from datetime import datetime

from app.domain.common import DomainValidationError, require_utc_datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarReadRequest:
    start: datetime
    end: datetime
    timezone: str

    def __post_init__(self) -> None:
        require_utc_datetime(self.start, "start")
        require_utc_datetime(self.end, "end")
        if self.start >= self.end:
            raise DomainValidationError("calendar read interval must be positive.")
        if not self.timezone.strip():
            raise DomainValidationError("timezone must not be blank.")


@dataclass(frozen=True, slots=True, kw_only=True, order=True)
class CalendarBusyInterval:
    start: datetime
    end: datetime
    transparency: str = "OPAQUE"

    def __post_init__(self) -> None:
        require_utc_datetime(self.start, "start")
        require_utc_datetime(self.end, "end")
        if self.start >= self.end:
            raise DomainValidationError("busy interval must be positive.")
        if self.transparency not in {"OPAQUE", "TRANSPARENT"}:
            raise DomainValidationError("unsupported calendar transparency.")
