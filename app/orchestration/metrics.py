"""Process-local development metrics; not a production monitoring backend."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class OrchestratorMetricsSnapshot:
    runs_created: int
    runs_completed: int
    runs_failed: int
    steps_claimed: int
    steps_succeeded: int
    step_attempts: int
    retries_scheduled: int
    leases_expired: int
    steps_reaped: int
    lease_conflicts: int
    waiting_user_count: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class OrchestratorMetrics:
    """Counters are synchronous because one event loop owns this Phase 2A object."""

    def __init__(self) -> None:
        self.runs_created = 0
        self.runs_completed = 0
        self.runs_failed = 0
        self.steps_claimed = 0
        self.steps_succeeded = 0
        self.step_attempts = 0
        self.retries_scheduled = 0
        self.leases_expired = 0
        self.steps_reaped = 0
        self.lease_conflicts = 0
        self.waiting_user_count = 0

    def snapshot(self) -> OrchestratorMetricsSnapshot:
        return OrchestratorMetricsSnapshot(
            runs_created=self.runs_created,
            runs_completed=self.runs_completed,
            runs_failed=self.runs_failed,
            steps_claimed=self.steps_claimed,
            steps_succeeded=self.steps_succeeded,
            step_attempts=self.step_attempts,
            retries_scheduled=self.retries_scheduled,
            leases_expired=self.leases_expired,
            steps_reaped=self.steps_reaped,
            lease_conflicts=self.lease_conflicts,
            waiting_user_count=self.waiting_user_count,
        )

    def reset(self) -> None:
        self.runs_created = 0
        self.runs_completed = 0
        self.runs_failed = 0
        self.steps_claimed = 0
        self.steps_succeeded = 0
        self.step_attempts = 0
        self.retries_scheduled = 0
        self.leases_expired = 0
        self.steps_reaped = 0
        self.lease_conflicts = 0
        self.waiting_user_count = 0
