from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.calendar_read.gateway import CalendarReadError
from app.calendar_read.http_provider import HttpCalendarReadProvider
from app.domain.calendar_read.models import CalendarReadRequest

pytestmark = pytest.mark.phase_6a


def _request() -> CalendarReadRequest:
    start = datetime(2026, 7, 20, tzinfo=UTC)
    return CalendarReadRequest(
        start=start, end=start + timedelta(days=1), timezone="UTC"
    )


@pytest.mark.asyncio
async def test_http_provider_discards_calendar_content() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer hidden-test-key"
        return httpx.Response(
            200,
            json={
                "busy": [
                    {
                        "start": "2026-07-20T10:00:00Z",
                        "end": "2026-07-20T11:00:00Z",
                        "transparency": "OPAQUE",
                        "title": "private",
                        "attendees": ["private"],
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpCalendarReadProvider(
        base_url="http://calendar.test",
        api_key="hidden-test-key",
        timeout_seconds=1,
        max_response_bytes=4096,
        client=client,
    )
    result = await provider.read_busy(_request())
    assert len(result) == 1
    assert not hasattr(result[0], "title")
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retryable", [(429, True), (500, True), (401, False)])
async def test_http_provider_classifies_status(status: int, retryable: bool) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"error": "safe"})
        )
    )
    provider = HttpCalendarReadProvider(
        base_url="http://calendar.test",
        api_key="key",
        timeout_seconds=1,
        max_response_bytes=4096,
        client=client,
    )
    with pytest.raises(CalendarReadError) as error:
        await provider.read_busy(_request())
    assert error.value.retryable is retryable
    await client.aclose()


@pytest.mark.asyncio
async def test_http_provider_rejects_malformed_schema() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"busy": [{"start": "bad"}]})
        )
    )
    provider = HttpCalendarReadProvider(
        base_url="http://calendar.test",
        api_key="key",
        timeout_seconds=1,
        max_response_bytes=4096,
        client=client,
    )
    with pytest.raises(CalendarReadError) as error:
        await provider.read_busy(_request())
    assert error.value.retryable is False
    await client.aclose()
