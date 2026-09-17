"""Bounded retry sleeper protocol."""

import asyncio
from typing import Protocol


class Sleeper(Protocol):
    async def sleep_ms(self, milliseconds: int) -> None: ...


class AsyncioSleeper:
    async def sleep_ms(self, milliseconds: int) -> None:
        await asyncio.sleep(milliseconds / 1000)


class RecordingSleeper:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def sleep_ms(self, milliseconds: int) -> None:
        self.calls.append(milliseconds)
