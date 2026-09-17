"""Injectable clock used by the bounded local alert evaluator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class AlertClock(Protocol):
    def now(self) -> datetime: ...


class SystemAlertClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualAlertClock:
    def __init__(self, initial: datetime | None = None) -> None:
        self._now = initial or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta.total_seconds() < 0:
            raise ValueError("Alert clock cannot move backwards.")
        self._now += delta
