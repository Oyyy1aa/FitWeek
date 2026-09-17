"""Single-process Schedule Draft counters without user content."""

from dataclasses import dataclass


@dataclass(slots=True)
class ScheduleMetrics:
    schedule_draft_requests: int = 0
    calendar_read_requests: int = 0
    calendar_read_successes: int = 0
    calendar_read_failures: int = 0
    calendar_manual_degraded: int = 0
    schedule_agent_primary_successes: int = 0
    schedule_agent_backup_successes: int = 0
    schedule_deterministic_fallbacks: int = 0
    schedule_complete_drafts: int = 0
    schedule_partial_drafts: int = 0
    schedule_conflicts_rejected: int = 0
    schedule_timezone_failures: int = 0
    schedule_idempotent_reuses: int = 0
    schedule_idempotency_conflicts: int = 0
    schedule_drafts_accepted: int = 0
    schedule_drafts_rejected: int = 0

    def as_dict(self) -> dict[str, int]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}
