"""Independent semaphore per explicitly registered tool version."""

import asyncio


class BulkheadManager:
    def __init__(self) -> None:
        self._semaphores: dict[tuple[str, str], asyncio.Semaphore] = {}
        self._limits: dict[tuple[str, str], int] = {}

    def semaphore(self, key: tuple[str, str], limit: int) -> asyncio.Semaphore:
        self._limits.setdefault(key, limit)
        return self._semaphores.setdefault(key, asyncio.Semaphore(limit))

    def active_count(self, key: tuple[str, str]) -> int:
        semaphore = self._semaphores.get(key)
        if semaphore is None:
            return 0
        return self._limits[key] - semaphore._value  # noqa: SLF001

    def available_permits(self, key: tuple[str, str]) -> int:
        semaphore = self._semaphores.get(key)
        return semaphore._value if semaphore is not None else 0  # noqa: SLF001
