"""HTTP free/busy reader that discards event content and keeps time only."""

from datetime import UTC, datetime
from typing import Any

import httpx

from app.calendar_read.gateway import CalendarReadError
from app.domain.calendar_read.models import CalendarBusyInterval, CalendarReadRequest


class HttpCalendarReadProvider:
    provider_name = "calendar-http"
    provider_version = "phase-6a-v1"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        max_response_bytes: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_bytes = max_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def read_busy(
        self, request: CalendarReadRequest
    ) -> tuple[CalendarBusyInterval, ...]:
        try:
            response = await self._client.post(
                f"{self._base_url}/busy",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "start": request.start.isoformat(),
                    "end": request.end.isoformat(),
                    "timezone": request.timezone,
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise CalendarReadError(
                "calendar provider unavailable", retryable=True
            ) from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise CalendarReadError(
                "calendar provider retryable failure", retryable=True
            )
        if response.status_code >= 400:
            raise CalendarReadError(
                "calendar provider rejected request", retryable=False
            )
        if len(response.content) > self._max_bytes:
            raise CalendarReadError("calendar response is oversized", retryable=False)
        try:
            document: Any = response.json()
            raw_busy = document["busy"]
            if not isinstance(raw_busy, list):
                raise TypeError
            intervals = tuple(self._parse(item) for item in raw_busy)
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarReadError(
                "calendar response schema invalid", retryable=False
            ) from exc
        return tuple(
            sorted(item for item in intervals if item.transparency == "OPAQUE")
        )

    @staticmethod
    def _parse(item: object) -> CalendarBusyInterval:
        if not isinstance(item, dict):
            raise TypeError
        start = datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(item["end"]).replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError
        transparency = str(item.get("transparency", "OPAQUE")).upper()
        return CalendarBusyInterval(
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
            transparency=transparency,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class ScriptedCalendarReadProvider:
    provider_name = "calendar-scripted"
    provider_version = "phase-6a-v1"

    def __init__(self, intervals: tuple[CalendarBusyInterval, ...] = ()) -> None:
        self._intervals = intervals
        self.calls = 0

    async def read_busy(
        self, request: CalendarReadRequest
    ) -> tuple[CalendarBusyInterval, ...]:
        self.calls += 1
        return tuple(
            item
            for item in self._intervals
            if item.start < request.end and request.start < item.end
        )

    async def close(self) -> None:
        return None
