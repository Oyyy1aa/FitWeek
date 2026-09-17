"""Freeze Recovery Candidates and all reproducibility references."""

import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.behavior.models import BehaviorSummary
from app.domain.context.models import ContextSnapshotReference
from app.domain.recovery.models import (
    CreateRecoveryDraftCommand,
    RecoveryActionCandidate,
    RecoveryActionCandidateSet,
    RecoveryChangeImpactSnapshot,
)
from app.domain.recovery.validation import (
    RECOVERY_AGENT_PROMPT_VERSION,
    RECOVERY_CANDIDATE_POLICY_VERSION,
)
from app.orchestration.clock import Clock


class RecoveryCandidateSetBuilder:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def build(
        self,
        *,
        user_id: UUID,
        command: CreateRecoveryDraftCommand,
        profile_version: int,
        constraint_versions: tuple[tuple[UUID, int], ...],
        summary: BehaviorSummary,
        context: ContextSnapshotReference,
        impact: RecoveryChangeImpactSnapshot,
        candidates: tuple[RecoveryActionCandidate, ...],
    ) -> RecoveryActionCandidateSet:
        payload = {
            "user": str(user_id),
            "request_type": command.request_type.value,
            "user_request_sha256": hashlib.sha256(
                command.user_request.encode("utf-8")
            ).hexdigest(),
            "targets": [str(item) for item in command.target_session_ids or ()],
            "root": str(command.root_plan_id),
            "revision": command.source_revision,
            "version": command.expected_plan_version,
            "profile_version": profile_version,
            "constraints": [
                (str(item), version) for item, version in constraint_versions
            ],
            "evidence": [item.fingerprint for item in summary.evidence_references],
            "behavior": summary.fingerprint,
            "context": context.context_fingerprint,
            "impact": impact.fingerprint,
            "candidates": [
                {
                    "id": str(item.id),
                    "type": item.action_type.value,
                    "target": str(item.target_session_id)
                    if item.target_session_id
                    else None,
                    "week": item.target_week_start.isoformat()
                    if item.target_week_start
                    else None,
                    "goal": item.redesign_goal.value if item.redesign_goal else None,
                }
                for item in candidates
            ],
            "policy": RECOVERY_CANDIDATE_POLICY_VERSION,
            "prompt": RECOVERY_AGENT_PROMPT_VERSION,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return RecoveryActionCandidateSet(
            id=uuid5(NAMESPACE_URL, f"recovery-candidate-set:{fingerprint}"),
            user_id=user_id,
            root_plan_id=command.root_plan_id,
            source_revision=command.source_revision,
            source_plan_version=command.expected_plan_version,
            behavior_summary_id=summary.id,
            behavior_summary_fingerprint=summary.fingerprint,
            context_snapshot_reference_id=context.id,
            context_fingerprint=context.context_fingerprint,
            change_impact_snapshot_id=impact.id,
            candidates=candidates,
            fingerprint=fingerprint,
            policy_version=RECOVERY_CANDIDATE_POLICY_VERSION,
            prompt_version=RECOVERY_AGENT_PROMPT_VERSION,
            created_at=self._clock.now(),
        )
