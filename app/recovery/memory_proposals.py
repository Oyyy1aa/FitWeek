"""Read-only, thresholded behavior proposal creation."""

from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from app.behavior.metrics import RecoveryMetrics
from app.domain.behavior.enums import (
    BehaviorConfidenceTier,
    BehaviorMemoryProposalStatus,
    BehaviorPatternType,
)
from app.domain.behavior.models import BehaviorMemoryProposal, BehaviorSummary
from app.domain.behavior.policies import BEHAVIOR_PROPOSAL_TTL_DAYS
from app.domain.memory.enums import MemoryType
from app.orchestration.clock import Clock


class BehaviorMemoryProposalBuilder:
    _TEMPORARY_MARKERS = (
        "只是本周",
        "本周临时",
        "临时有事",
        "this week only",
        "temporary this week",
        "just this week",
    )

    def __init__(self, clock: Clock, metrics: RecoveryMetrics) -> None:
        self._clock = clock
        self._metrics = metrics

    def build(
        self,
        *,
        user_id: UUID,
        summary: BehaviorSummary,
        user_request: str,
        proposal_scope: str,
    ) -> tuple[BehaviorMemoryProposal, ...]:
        normalized = user_request.casefold()
        if any(item in normalized for item in self._TEMPORARY_MARKERS):
            return ()
        now = self._clock.now()
        proposals: list[BehaviorMemoryProposal] = []
        patterns = {
            item.pattern_id: item
            for group in (
                summary.repeated_time_patterns,
                summary.repeated_location_patterns,
                summary.repeated_skip_patterns,
            )
            for item in group
        }
        for pattern in sorted(patterns.values(), key=lambda item: item.pattern_id):
            definition: tuple[MemoryType, str] | None = None
            if (
                pattern.pattern_type
                is BehaviorPatternType.REPEATED_COMPLETION_TIME_OF_DAY
            ):
                definition = (MemoryType.PREFERRED_TIME_OF_DAY, "preferred_time_of_day")
            elif pattern.pattern_type is BehaviorPatternType.REPEATED_SKIP_TIME_OF_DAY:
                definition = (
                    MemoryType.PREFERRED_TIME_OF_DAY,
                    "review_avoid_time_of_day",
                )
            elif (
                pattern.pattern_type is BehaviorPatternType.REPEATED_COMPLETION_LOCATION
            ):
                definition = (MemoryType.PREFERRED_LOCATION, "preferred_location")
            elif pattern.pattern_type is BehaviorPatternType.REPEATED_SKIP_LOCATION:
                definition = (MemoryType.PREFERRED_LOCATION, "review_avoid_location")
            if definition is None:
                continue
            memory_type, key = definition
            proposal_id = uuid5(
                NAMESPACE_URL,
                (
                    f"behavior-proposal:{user_id}:{summary.fingerprint}:"
                    f"{pattern.pattern_id}:{proposal_scope}"
                ),
            )
            proposals.append(
                BehaviorMemoryProposal(
                    id=proposal_id,
                    user_id=user_id,
                    memory_type=memory_type,
                    proposed_key=key,
                    proposed_value=pattern.key,
                    behavior_pattern_ids=(pattern.pattern_id,),
                    evidence_checkin_ids=pattern.evidence_ids,
                    confidence_tier=(
                        BehaviorConfidenceTier.STRONG
                        if pattern.ratio >= 0.8
                        else BehaviorConfidenceTier.THRESHOLD
                    ),
                    status=BehaviorMemoryProposalStatus.PROPOSED,
                    created_at=now,
                    expires_at=now + timedelta(days=BEHAVIOR_PROPOSAL_TTL_DAYS),
                )
            )
        result = tuple(proposals)
        self._metrics.behavior_memory_proposals_created += len(result)
        return result
