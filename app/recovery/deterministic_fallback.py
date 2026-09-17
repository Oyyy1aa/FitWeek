"""Safe deterministic selection from the frozen Candidate Set only."""

from uuid import UUID

from app.domain.recovery.enums import RecoveryActionType, RecoveryRequestType
from app.domain.recovery.models import (
    RecoveryActionCandidateSet,
    RecoveryAgentOutput,
    RecoveryChangeImpactSnapshot,
)


class DeterministicRecoveryFallback:
    _PREFERRED = {
        RecoveryRequestType.RESCHEDULE_REQUEST: (
            RecoveryActionType.REQUEST_SESSION_RESCHEDULE
        ),
        RecoveryRequestType.REDUCE_FUTURE_LOAD: (
            RecoveryActionType.REQUEST_SESSION_REDESIGN
        ),
        RecoveryRequestType.REPLACE_FUTURE_SESSION: (
            RecoveryActionType.REQUEST_SESSION_REDESIGN
        ),
        RecoveryRequestType.REMOVE_FUTURE_SESSION: (
            RecoveryActionType.REMOVE_FUTURE_SESSION
        ),
        RecoveryRequestType.NEXT_WEEK_REVIEW: (
            RecoveryActionType.NEXT_WEEK_FREQUENCY_REVIEW
        ),
        RecoveryRequestType.GENERAL_RECOVERY_REVIEW: (
            RecoveryActionType.KEEP_CURRENT_PLAN
        ),
    }

    def build(
        self,
        candidate_set: RecoveryActionCandidateSet,
        *,
        request_type: RecoveryRequestType,
        impact: RecoveryChangeImpactSnapshot,
    ) -> RecoveryAgentOutput:
        action_type = self._PREFERRED[request_type]
        matches = tuple(
            item for item in candidate_set.candidates if item.action_type is action_type
        )
        if matches:
            selected: tuple[UUID, ...]
            if action_type in {
                RecoveryActionType.KEEP_CURRENT_PLAN,
                RecoveryActionType.NEXT_WEEK_FREQUENCY_REVIEW,
            }:
                selected = (matches[0].id,)
            else:
                selected_by_session: dict[UUID | None, UUID] = {}
                for item in matches:
                    selected_by_session.setdefault(item.target_session_id, item.id)
                selected = tuple(selected_by_session.values())
            return RecoveryAgentOutput(
                selected_action_candidate_ids=selected,
                unresolved_session_ids=(),
                explanation_summary=(
                    "A deterministic choice was made from the frozen safe actions."
                ),
            )
        keep = next(
            (
                item
                for item in candidate_set.candidates
                if item.action_type is RecoveryActionType.KEEP_CURRENT_PLAN
            ),
            None,
        )
        explicit_change = request_type not in {
            RecoveryRequestType.GENERAL_RECOVERY_REVIEW,
            RecoveryRequestType.NEXT_WEEK_REVIEW,
        }
        return RecoveryAgentOutput(
            selected_action_candidate_ids=(keep.id,)
            if keep and not explicit_change
            else (),
            unresolved_session_ids=(
                impact.mutable_session_ids if explicit_change else ()
            ),
            explanation_summary=(
                "No safe frozen action could satisfy the explicit request."
                if explicit_change
                else "The current plan is kept because no safe change is needed."
            ),
        )
