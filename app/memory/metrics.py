"""Process-local Phase 4A counters; these are not production monitoring."""

from dataclasses import asdict, dataclass
from typing import Self


@dataclass(slots=True)
class MemoryMetricsSnapshot:
    memories_created: int = 0
    memories_updated: int = 0
    memories_deleted: int = 0
    memories_replaced: int = 0
    memory_candidates_created: int = 0
    memory_candidates_accepted: int = 0
    memory_candidates_rejected: int = 0
    memory_candidates_expired: int = 0
    memory_queries: int = 0
    memory_query_failures: int = 0
    context_builds: int = 0
    context_build_failures: int = 0
    context_no_memory_degraded: int = 0
    expired_memories_filtered: int = 0
    deleted_memories_filtered: int = 0
    pending_memories_filtered: int = 0
    memory_conflicts_resolved: int = 0
    context_budget_rejections: int = 0
    profile_agent_context_builds: int = 0
    plan_generation_context_builds: int = 0
    context_snapshots_created: int = 0
    context_snapshots_reused: int = 0
    context_snapshot_failures: int = 0
    profile_agent_no_memory_degraded: int = 0
    plan_generation_no_memory_degraded: int = 0
    draft_memory_candidate_previews: int = 0
    draft_memory_candidates_imported: int = 0
    draft_memory_candidate_import_conflicts: int = 0
    memories_injected_into_profile_agent: int = 0
    memories_injected_into_plan_generation: int = 0
    memories_shadowed_by_current_task: int = 0
    memories_shadowed_by_constraints: int = 0
    memory_prompt_injection_cases_safely_encoded: int = 0
    memory_cache_degraded: int = 0

    def copy(self) -> Self:
        return type(self)(**asdict(self))

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class MemoryMetrics:
    def __init__(self) -> None:
        self._values = MemoryMetricsSnapshot()

    def increment(self, name: str, amount: int = 1) -> None:
        current = getattr(self._values, name)
        setattr(self._values, name, current + amount)

    def snapshot(self) -> MemoryMetricsSnapshot:
        return self._values.copy()
