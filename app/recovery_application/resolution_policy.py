"""Pure mapping from frozen Recovery Candidates to executable subflows."""

from dataclasses import dataclass
from uuid import UUID

from app.application.errors import (
    RecoveryConflictingActions,
    RecoveryInvalidActionSelection,
)
from app.domain.recovery.enums import RecoveryActionType
from app.domain.recovery.models import RecoveryActionCandidate, RecoveryDraft
from app.domain.recovery_application.models import RecoveryActionPreview


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedRecoveryActions:
    actions: tuple[RecoveryActionCandidate, ...]
    previews: tuple[RecoveryActionPreview, ...]
    remove_session_ids: tuple[UUID, ...]
    redesign_session_ids: tuple[UUID, ...]
    reschedule_session_ids: tuple[UUID, ...]
    preserve_session_ids: tuple[UUID, ...]
    no_change: bool
    next_week_review: bool
    requires_calendar_reconciliation: bool


class RecoveryActionResolutionPolicy:
    version = "recovery-action-resolution-v1"

    def resolve(
        self,
        *,
        draft: RecoveryDraft,
        candidates: tuple[RecoveryActionCandidate, ...],
        selected_ids: tuple[UUID, ...],
        plan_session_ids: tuple[UUID, ...],
    ) -> ResolvedRecoveryActions:
        candidate_map = {item.id: item for item in candidates}
        selected_set = set(selected_ids)
        if selected_set != set(draft.selected_action_candidate_ids):
            raise RecoveryInvalidActionSelection(
                "Apply must use the exact action selection frozen in the "
                "accepted Draft."
            )
        if any(item not in candidate_map for item in selected_ids):
            raise RecoveryInvalidActionSelection(
                "A selected Recovery action is outside the frozen Candidate Set."
            )
        actions = tuple(
            sorted(
                (candidate_map[item] for item in selected_ids),
                key=lambda item: str(item.id),
            )
        )
        keep = any(
            item.action_type is RecoveryActionType.KEEP_CURRENT_PLAN for item in actions
        )
        next_week = any(
            item.action_type is RecoveryActionType.NEXT_WEEK_FREQUENCY_REVIEW
            for item in actions
        )
        if keep and len(actions) != 1:
            raise RecoveryConflictingActions(
                "KEEP cannot be combined with another action."
            )
        if next_week and len(actions) != 1:
            raise RecoveryConflictingActions(
                "NEXT_WEEK_REVIEW cannot mutate the current Plan."
            )
        by_session: dict[UUID, set[RecoveryActionType]] = {}
        for item in actions:
            if item.target_session_id is None:
                continue
            by_session.setdefault(item.target_session_id, set()).add(item.action_type)
        allowed_pair = {
            RecoveryActionType.REQUEST_SESSION_REDESIGN,
            RecoveryActionType.REQUEST_SESSION_RESCHEDULE,
        }
        if any(
            RecoveryActionType.REMOVE_FUTURE_SESSION in action_types
            and len(action_types) > 1
            for action_types in by_session.values()
        ) or any(
            len(action_types) > 1 and action_types != allowed_pair
            for action_types in by_session.values()
        ):
            raise RecoveryConflictingActions(
                "REMOVE conflicts with redesign or reschedule on the same Session."
            )
        remove = self._targets(actions, RecoveryActionType.REMOVE_FUTURE_SESSION)
        redesign = self._targets(actions, RecoveryActionType.REQUEST_SESSION_REDESIGN)
        reschedule = self._targets(
            actions, RecoveryActionType.REQUEST_SESSION_RESCHEDULE
        )
        changed = set(remove) | set(redesign) | set(reschedule)
        preserved = tuple(sorted(set(plan_session_ids) - changed, key=str))
        previews = tuple(
            RecoveryActionPreview(
                candidate_id=item.id,
                action_type=item.action_type,
                target_session_id=item.target_session_id,
                requires_session_design_draft=item.requires_session_design_draft,
                requires_schedule_draft=item.requires_schedule_draft,
                requires_plan_revision=item.requires_plan_revision,
                requires_calendar_reconciliation=(
                    item.requires_calendar_reconciliation
                ),
            )
            for item in actions
        )
        return ResolvedRecoveryActions(
            actions=actions,
            previews=previews,
            remove_session_ids=remove,
            redesign_session_ids=redesign,
            reschedule_session_ids=reschedule,
            preserve_session_ids=preserved,
            no_change=keep,
            next_week_review=next_week,
            requires_calendar_reconciliation=any(
                item.requires_calendar_reconciliation for item in actions
            ),
        )

    @staticmethod
    def _targets(
        actions: tuple[RecoveryActionCandidate, ...], action_type: RecoveryActionType
    ) -> tuple[UUID, ...]:
        return tuple(
            sorted(
                (
                    item.target_session_id
                    for item in actions
                    if item.action_type is action_type
                    and item.target_session_id is not None
                ),
                key=str,
            )
        )
