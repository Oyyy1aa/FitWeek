"""Bounded single-process concurrency and rate controls."""

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from app.domain.model_gateway.errors import (
    ModelProviderUnavailableError,
    ModelRateLimitedError,
)


class ProcessLocalModelLimiter:
    """Limit only this process; no distributed guarantee is implied."""

    def __init__(
        self,
        *,
        max_concurrency: int,
        rate_per_minute: int,
        acquire_timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rate_per_minute = rate_per_minute
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._clock = clock
        self._timestamps: deque[float] = deque()
        self._rate_lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self._check_rate_limit()
        try:
            async with asyncio.timeout(self._acquire_timeout_seconds):
                await self._semaphore.acquire()
        except TimeoutError as exc:
            raise ModelProviderUnavailableError(
                "The model concurrency queue timed out."
            ) from exc
        try:
            yield
        finally:
            self._semaphore.release()

    async def _check_rate_limit(self) -> None:
        now = self._clock()
        async with self._rate_lock:
            while self._timestamps and now - self._timestamps[0] >= 60:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._rate_per_minute:
                raise ModelRateLimitedError(
                    "The local model request rate was exceeded."
                )
            self._timestamps.append(now)
