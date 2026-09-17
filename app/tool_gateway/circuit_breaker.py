"""Process-local, per-tool circuit state with a single half-open probe."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.domain.tools.enums import CircuitState, ToolErrorCategory
from app.reliability.clock import Clock


@dataclass(frozen=True, slots=True)
class CircuitKey:
    tool_id: str
    tool_version: str
    provider_name: str
    operation_class: str


@dataclass(slots=True)
class _CircuitRecord:
    state: CircuitState = CircuitState.CLOSED
    failures: list[datetime] = field(default_factory=list)
    opened_at: datetime | None = None
    probe_in_flight: bool = False


class CircuitBreaker:
    def __init__(
        self,
        clock: Clock,
        *,
        failure_threshold: int = 5,
        failure_window_seconds: int = 60,
        open_duration_seconds: int = 30,
    ) -> None:
        self._clock = clock
        self._threshold = failure_threshold
        self._window = timedelta(seconds=failure_window_seconds)
        self._open_duration = timedelta(seconds=open_duration_seconds)
        self._records: dict[CircuitKey, _CircuitRecord] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: CircuitKey) -> tuple[bool, CircuitState]:
        async with self._lock:
            record = self._records.setdefault(key, _CircuitRecord())
            now = self._clock.now()
            if record.state is CircuitState.OPEN:
                if (
                    record.opened_at is not None
                    and now - record.opened_at >= self._open_duration
                ):
                    record.state = CircuitState.HALF_OPEN
                else:
                    return False, CircuitState.OPEN
            if record.state is CircuitState.HALF_OPEN:
                if record.probe_in_flight:
                    return False, CircuitState.HALF_OPEN
                record.probe_in_flight = True
            return True, record.state

    async def record_success(self, key: CircuitKey) -> CircuitState:
        async with self._lock:
            record = self._records.setdefault(key, _CircuitRecord())
            record.failures.clear()
            record.probe_in_flight = False
            record.opened_at = None
            record.state = CircuitState.CLOSED
            return record.state

    async def record_failure(
        self, key: CircuitKey, category: ToolErrorCategory
    ) -> CircuitState:
        if category not in {
            ToolErrorCategory.TIMEOUT,
            ToolErrorCategory.CONNECTION,
            ToolErrorCategory.RATE_LIMIT,
            ToolErrorCategory.UPSTREAM_5XX,
        }:
            return await self.state(key)
        async with self._lock:
            record = self._records.setdefault(key, _CircuitRecord())
            now = self._clock.now()
            record.probe_in_flight = False
            if record.state is CircuitState.HALF_OPEN:
                record.state = CircuitState.OPEN
                record.opened_at = now
                return record.state
            record.failures = [
                item for item in record.failures if now - item <= self._window
            ]
            record.failures.append(now)
            if len(record.failures) >= self._threshold:
                record.state = CircuitState.OPEN
                record.opened_at = now
            return record.state

    async def state(self, key: CircuitKey) -> CircuitState:
        async with self._lock:
            return self._records.get(key, _CircuitRecord()).state

    async def failure_count(self, key: CircuitKey) -> int:
        async with self._lock:
            record = self._records.get(key)
            if record is None:
                return 0
            now = self._clock.now()
            return len([item for item in record.failures if now - item <= self._window])

    async def open_keys(self) -> tuple[str, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    f"{key.tool_id}:{key.tool_version}:{key.provider_name}:{key.operation_class}"
                    for key, record in self._records.items()
                    if record.state is CircuitState.OPEN
                )
            )
