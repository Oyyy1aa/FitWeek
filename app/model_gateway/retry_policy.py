"""Bounded provider ordering and injectable retry sleeping."""

import asyncio
from typing import Protocol


class Sleeper(Protocol):
    async def sleep(self, seconds: float) -> None: ...


class AsyncioSleeper:
    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ModelRetryPolicy:
    """Phase 3A policy: primary at most twice, backup at most once."""

    def __init__(self, *, max_attempts: int, retry_delay_seconds: float = 0.05) -> None:
        self.max_attempts = min(max_attempts, 3)
        self.primary_attempts = min(2, self.max_attempts)
        self.backup_attempts = 1 if self.max_attempts >= 2 else 0
        self.retry_delay_seconds = retry_delay_seconds
