"""Bounded deterministic retry scheduling."""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    max_attempts: int = 3
    delays_seconds: tuple[int, ...] = (0, 1, 2)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive.")
        if not self.delays_seconds or any(value < 0 for value in self.delays_seconds):
            raise ValueError("delays_seconds must contain non-negative values.")

    def retry_at(self, *, attempt_no: int, now: datetime) -> datetime | None:
        if attempt_no >= self.max_attempts:
            return None
        index = min(max(attempt_no - 1, 0), len(self.delays_seconds) - 1)
        return now + timedelta(seconds=self.delays_seconds[index])
