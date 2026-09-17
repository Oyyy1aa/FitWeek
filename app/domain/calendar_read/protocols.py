"""Port for privacy-minimal calendar free/busy reads."""

from typing import Protocol

from app.domain.calendar_read.models import CalendarBusyInterval, CalendarReadRequest


class CalendarReadProvider(Protocol):
    provider_name: str
    provider_version: str

    async def read_busy(
        self, request: CalendarReadRequest
    ) -> tuple[CalendarBusyInterval, ...]: ...

    async def close(self) -> None: ...
