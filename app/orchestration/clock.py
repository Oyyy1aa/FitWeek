"""Injectable UTC clocks for scheduling without real test sleeps."""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Explicitly advanced clock for deterministic lease and retry tests."""

    def __init__(self, current: datetime) -> None:
        if current.tzinfo is None or current.utcoffset() != timedelta(0):
            raise ValueError("FakeClock requires an aware UTC datetime.")
        self._current = current

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> datetime:
        if delta.total_seconds() < 0:
            raise ValueError("FakeClock cannot move backwards.")
        self._current += delta
        return self._current
