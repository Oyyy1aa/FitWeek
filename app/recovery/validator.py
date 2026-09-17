"""Validate model selections against immutable Recovery candidates."""

from app.domain.plans.models import WeeklyPlan
from app.domain.recovery.enums import RecoveryActionType
from app.domain.recovery.models import (
    RecoveryActionCandidate,
    RecoveryActionCandidateSet,
    RecoveryAgentOutput,
    RecoveryChangeImpactSnapshot,
)
from app.recovery.spacing_validator import RecoverySpacingValidator


class RecoveryAgentBusinessValidator:
    def __init__(self, spacing: RecoverySpacingValidator | None = None) -> None:
        self._spacing = spacing or RecoverySpacingValidator()

    def validate(
        self,
        output: RecoveryAgentOutput,
        *,
        candidate_set: RecoveryActionCandidateSet,
        impact: RecoveryChangeImpactSnapshot,
        plan: WeeklyPlan,
        check_spacing: bool = True,
    ) -> RecoveryAgentOutput:
        allowed = candidate_set.candidate_map
        if any(item not in allowed for item in output.selected_action_candidate_ids):
            raise ValueError("RECOVERY_ACTION_NOT_IN_CANDIDATE_SET")
        selected = tuple(allowed[item] for item in output.selected_action_candidate_ids)
        self._validate_conflicts(selected)
        mutable = set(impact.mutable_session_ids)
        if any(
            item.target_session_id is not None and item.target_session_id not in mutable
            for item in selected
        ):
            raise ValueError("RECOVERY_TARGET_SESSION_IMMUTABLE")
        if check_spacing:
            spacing = self._spacing.validate(plan=plan, selected=selected)
            if not spacing.passed:
                raise ValueError(spacing.violation_codes[0])
        if any(item not in mutable for item in output.unresolved_session_ids):
            raise ValueError("RECOVERY_TARGET_SESSION_IMMUTABLE")
        return output

    @staticmethod
    def _validate_conflicts(selected: tuple[RecoveryActionCandidate, ...]) -> None:
        if any(
            item.action_type is RecoveryActionType.KEEP_CURRENT_PLAN
            for item in selected
        ):
            if len(selected) > 1:
                raise ValueError("RECOVERY_CONFLICTING_ACTIONS")
        by_session: dict[object, set[RecoveryActionType]] = {}
        for item in selected:
            if item.target_session_id is None:
                continue
            actions = by_session.setdefault(item.target_session_id, set())
            if item.action_type in actions:
                raise ValueError("RECOVERY_DUPLICATE_SESSION_ACTION")
            actions.add(item.action_type)
        allowed_pair = {
            RecoveryActionType.REQUEST_SESSION_REDESIGN,
            RecoveryActionType.REQUEST_SESSION_RESCHEDULE,
        }
        if any(
            len(actions) > 1 and actions != allowed_pair
            for actions in by_session.values()
        ):
            raise ValueError("RECOVERY_CONFLICTING_ACTIONS")
