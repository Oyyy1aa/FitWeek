"""Process-local Phase 7A counters."""

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class RecoveryMetrics:
    behavior_summaries_built: int = 0
    behavior_checkins_deduplicated: int = 0
    behavior_conflicts_detected: int = 0
    behavior_patterns_created: int = 0
    behavior_memory_proposals_created: int = 0
    recovery_draft_requests: int = 0
    recovery_scope_blocks: int = 0
    recovery_scope_reviews: int = 0
    recovery_primary_successes: int = 0
    recovery_backup_successes: int = 0
    recovery_deterministic_fallbacks: int = 0
    recovery_complete_drafts: int = 0
    recovery_partial_drafts: int = 0
    recovery_no_change_drafts: int = 0
    recovery_action_required_drafts: int = 0
    recovery_idempotent_reuses: int = 0
    recovery_idempotency_conflicts: int = 0
    recovery_drafts_accepted: int = 0
    recovery_drafts_rejected: int = 0

    def snapshot(self) -> dict[str, int]:
        return asdict(self)
