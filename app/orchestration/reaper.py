"""Lease-expiry recovery for the current Python process."""

from dataclasses import dataclass
from uuid import UUID

from app.domain.orchestration.repositories import OrchestrationRepository
from app.orchestration.clock import Clock
from app.orchestration.retry_policy import RetryPolicy


@dataclass(frozen=True, slots=True, kw_only=True)
class ReaperResult:
    reaped_step_ids: tuple[UUID, ...]


class StepReaper:
    def __init__(
        self,
        *,
        repository: OrchestrationRepository,
        clock: Clock,
        retry_policy: RetryPolicy,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._retry_policy = retry_policy

    async def run_once(self) -> ReaperResult:
        reaped = await self._repository.reap_expired_steps(
            now=self._clock.now(),
            delays_seconds=self._retry_policy.delays_seconds,
        )
        return ReaperResult(reaped_step_ids=tuple(item.id for item in reaped))
