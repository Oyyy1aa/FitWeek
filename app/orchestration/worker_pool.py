"""Lifespan-owned cooperative worker and reaper tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.orchestration.reaper import StepReaper
from app.orchestration.worker import OrchestrationWorker


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerPoolStatus:
    enabled: bool
    running: bool
    task_count: int
    failed_tasks: int


class OrchestrationWorkerPool:
    def __init__(
        self,
        *,
        workers: tuple[OrchestrationWorker, ...],
        reaper: StepReaper,
        reaper_interval_seconds: float,
    ) -> None:
        if not workers:
            raise ValueError("At least one worker is required.")
        if reaper_interval_seconds <= 0:
            raise ValueError("reaper interval must be positive.")
        self._workers = workers
        self._reaper = reaper
        self._reaper_interval_seconds = reaper_interval_seconds
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop_event.clear()
        self._tasks = [
            asyncio.create_task(
                worker.run_loop(self._stop_event),
                name=f"fitweek-orchestrator-{worker.worker_id}",
            )
            for worker in self._workers
        ]
        self._tasks.append(
            asyncio.create_task(
                self._reaper_loop(),
                name="fitweek-orchestrator-reaper",
            )
        )
        await asyncio.sleep(0)
        failures = [task for task in self._tasks if task.done() and task.exception()]
        if failures:
            await self.stop()
            raise RuntimeError("Orchestrator worker pool failed during startup.")

    async def stop(self) -> None:
        if not self._tasks:
            return
        self._stop_event.set()
        tasks = tuple(self._tasks)
        try:
            async with asyncio.timeout(5):
                await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._tasks.clear()

    def status(self) -> WorkerPoolStatus:
        failures = sum(
            1
            for task in self._tasks
            if task.done() and not task.cancelled() and task.exception() is not None
        )
        return WorkerPoolStatus(
            enabled=True,
            running=bool(self._tasks) and failures == 0,
            task_count=len(self._tasks),
            failed_tasks=failures,
        )

    async def _reaper_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._reaper.run_once()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._reaper_interval_seconds,
                )
            except TimeoutError:
                continue
