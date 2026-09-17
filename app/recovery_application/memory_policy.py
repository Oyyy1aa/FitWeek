"""Deterministic gate for importing Recovery behavior proposals."""

from collections.abc import Sequence
from datetime import datetime

from app.domain.behavior.enums import BehaviorMemoryProposalStatus
from app.domain.behavior.models import BehaviorMemoryProposal, BehaviorSummary
from app.domain.behavior.policies import MIN_SIGNAL_OCCURRENCES
from app.domain.memory.models import UserMemory
from app.domain.recovery_application.models import (
    RecoveryMemoryProposalPreviewItem,
)


class BehaviorMemoryProposalImportPolicy:
    """Evaluate frozen evidence without mutating Memory or Recovery state."""

    def evaluate(
        self,
        *,
        proposal: BehaviorMemoryProposal,
        summary: BehaviorSummary,
        active_memories: Sequence[UserMemory],
        now: datetime,
    ) -> RecoveryMemoryProposalPreviewItem:
        conflict_checkins = set(summary.conflict_checkin_ids) & set(
            proposal.evidence_checkin_ids
        )
        duplicate = any(
            item.memory_type is proposal.memory_type
            and item.key == proposal.proposed_key
            and item.normalized_value == proposal.proposed_value
            for item in active_memories
        )
        conflict = any(
            item.memory_type is proposal.memory_type
            and item.key == proposal.proposed_key
            and item.normalized_value != proposal.proposed_value
            for item in active_memories
        )
        reasons: list[str] = []
        if proposal.status is not BehaviorMemoryProposalStatus.PROPOSED:
            reasons.append("PROPOSAL_NOT_ACTIVE")
        if len(proposal.evidence_checkin_ids) < MIN_SIGNAL_OCCURRENCES:
            reasons.append("INSUFFICIENT_BEHAVIOR_EVIDENCE")
        if conflict_checkins:
            reasons.append("CONFLICTING_CHECK_IN_EVIDENCE")
        if duplicate:
            reasons.append("ACTIVE_MEMORY_DUPLICATE")
        if conflict:
            reasons.append("ACTIVE_MEMORY_CONFLICT")
        if proposal.expires_at <= now:
            reasons.append("PROPOSAL_EXPIRED")
        return RecoveryMemoryProposalPreviewItem(
            proposal_id=proposal.id,
            memory_type=proposal.memory_type.value,
            proposed_key=proposal.proposed_key,
            proposed_value=proposal.proposed_value,
            evidence_count=len(proposal.evidence_checkin_ids),
            duplicate=duplicate,
            conflict=conflict,
            importable=not reasons,
            reason_codes=tuple(sorted(reasons)),
        )
