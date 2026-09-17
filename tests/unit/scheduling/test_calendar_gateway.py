from datetime import UTC, datetime, timedelta

import pytest

from app.calendar_read.gateway import CalendarReadError, CalendarReadGateway
from app.domain.calendar_read.models import CalendarBusyInterval, CalendarReadRequest
from app.domain.scheduling.enums import CalendarReadMode

pytestmark = pytest.mark.phase_6a


class Provider:
    provider_name = "test"
    provider_version = "v1"

    def __init__(self, failures: int = 0, retryable: bool = True) -> None:
        self.failures, self.retryable, self.calls = failures, retryable, 0

    async def read_busy(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise CalendarReadError("no", retryable=self.retryable)
        return (
            CalendarBusyInterval(
                start=request.start, end=request.start + timedelta(minutes=1)
            ),
        )

    async def close(self):
        return None


def request():
    start = datetime(2026, 7, 20, tzinfo=UTC)
    return CalendarReadRequest(
        start=start, end=start + timedelta(days=1), timezone="UTC"
    )


@pytest.mark.asyncio
async def test_disabled_does_not_call_provider() -> None:
    provider = Provider()
    result = await CalendarReadGateway(provider=provider, enabled=False).read_busy(
        request()
    )
    assert result.mode is CalendarReadMode.DISABLED
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_retryable_failure_is_bounded_then_succeeds() -> None:
    provider = Provider(failures=1)
    result = await CalendarReadGateway(
        provider=provider, enabled=True, max_attempts=2
    ).read_busy(request())
    assert result.mode is CalendarReadMode.PROVIDER
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_failure_degrades_to_manual_only() -> None:
    provider = Provider(failures=3)
    result = await CalendarReadGateway(
        provider=provider, enabled=True, max_attempts=2
    ).read_busy(request())
    assert result.mode is CalendarReadMode.MANUAL_ONLY
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_non_retryable_failure_has_one_attempt() -> None:
    provider = Provider(failures=3, retryable=False)
    result = await CalendarReadGateway(
        provider=provider, enabled=True, max_attempts=2
    ).read_busy(request())
    assert result.mode is CalendarReadMode.MANUAL_ONLY
    assert provider.calls == 1
