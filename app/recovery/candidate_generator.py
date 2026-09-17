"""Deterministically enumerate safe action choices; no model access."""

from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.behavior.models import BehaviorSummary
from app.domain.plans.models import WeeklyPlan
from app.domain.recovery.enums import (
    RecoveryActionType,
    RecoveryRedesignGoal,
    RecoveryRequestType,
)
from app.domain.recovery.models import (
    RecoveryActionCandidate,
    RecoveryChangeImpactSnapshot,
)


def _candidate_id(*parts: object) -> UUID:
    return uuid5(NAMESPACE_URL, "recovery-candidate:" + ":".join(map(str, parts)))


class RecoveryCandidateGenerator:
    def generate(
        self,
        *,
        request_type: RecoveryRequestType,
        target_session_ids: tuple[UUID, ...] | None,
        plan: WeeklyPlan,
        impact: RecoveryChangeImpactSnapshot,
        behavior: BehaviorSummary,
    ) -> tuple[RecoveryActionCandidate, ...]:
        pattern_ids = tuple(
            sorted(
                {
                    item.pattern_id
                    for group in (
                        behavior.repeated_time_patterns,
                        behavior.repeated_location_patterns,
                        behavior.repeated_skip_patterns,
                    )
                    for item in group
                }
            )
        )
        values: list[RecoveryActionCandidate] = [
            self._make(
                impact=impact,
                action=RecoveryActionType.KEEP_CURRENT_PLAN,
                target=None,
                week=None,
                goal=None,
                evidence=pattern_ids,
                rank=0,
            )
        ]
        targets = tuple(
            item
            for item in (target_session_ids or impact.mutable_session_ids)
            if item in set(impact.mutable_session_ids)
        )
        rank = 10
        definitions: tuple[
            tuple[RecoveryActionType, RecoveryRedesignGoal | None], ...
        ] = ()
        if request_type is RecoveryRequestType.RESCHEDULE_REQUEST:
            definitions = ((RecoveryActionType.REQUEST_SESSION_RESCHEDULE, None),)
        elif request_type is RecoveryRequestType.REDUCE_FUTURE_LOAD:
            definitions = (
                (
                    RecoveryActionType.REQUEST_SESSION_REDESIGN,
                    RecoveryRedesignGoal.LOWER_LOAD,
                ),
                (
                    RecoveryActionType.REQUEST_SESSION_REDESIGN,
                    RecoveryRedesignGoal.SHORTER_DURATION,
                ),
            )
        elif request_type is RecoveryRequestType.REPLACE_FUTURE_SESSION:
            definitions = (
                (
                    RecoveryActionType.REQUEST_SESSION_REDESIGN,
                    RecoveryRedesignGoal.LOW_IMPACT,
                ),
            )
        elif request_type is RecoveryRequestType.REMOVE_FUTURE_SESSION:
            definitions = ((RecoveryActionType.REMOVE_FUTURE_SESSION, None),)
        elif request_type is RecoveryRequestType.GENERAL_RECOVERY_REVIEW:
            if behavior.repeated_skip_patterns:
                definitions = (
                    (RecoveryActionType.REQUEST_SESSION_RESCHEDULE, None),
                    (
                        RecoveryActionType.REQUEST_SESSION_REDESIGN,
                        RecoveryRedesignGoal.LOWER_LOAD,
                    ),
                )

        for action, goal in definitions:
            for session_id in targets:
                if (
                    action is RecoveryActionType.REMOVE_FUTURE_SESSION
                    and impact.weekly_frequency_before - 1
                    < impact.minimum_allowed_frequency
                ):
                    continue
                values.append(
                    self._make(
                        impact=impact,
                        action=action,
                        target=session_id,
                        week=None,
                        goal=goal,
                        evidence=pattern_ids,
                        rank=rank,
                    )
                )
                rank += 1
        if request_type in {
            RecoveryRequestType.NEXT_WEEK_REVIEW,
            RecoveryRequestType.GENERAL_RECOVERY_REVIEW,
        }:
            next_week = plan.week_start + timedelta(days=7)
            values.append(
                self._make(
                    impact=impact,
                    action=RecoveryActionType.NEXT_WEEK_FREQUENCY_REVIEW,
                    target=None,
                    week=next_week,
                    goal=None,
                    evidence=pattern_ids,
                    rank=90,
                )
            )
        return tuple(
            sorted(values, key=lambda item: (item.deterministic_rank, str(item.id)))
        )

    @staticmethod
    def _make(
        *,
        impact: RecoveryChangeImpactSnapshot,
        action: RecoveryActionType,
        target: UUID | None,
        week: object,
        goal: RecoveryRedesignGoal | None,
        evidence: tuple[str, ...],
        rank: int,
    ) -> RecoveryActionCandidate:
        candidate_id = _candidate_id(
            impact.fingerprint,
            action.value,
            target or "none",
            week or "none",
            goal.value if goal else "none",
        )
        schedule = action is RecoveryActionType.REQUEST_SESSION_RESCHEDULE
        redesign = action is RecoveryActionType.REQUEST_SESSION_REDESIGN
        revision = action in {
            RecoveryActionType.REQUEST_SESSION_RESCHEDULE,
            RecoveryActionType.REQUEST_SESSION_REDESIGN,
            RecoveryActionType.REMOVE_FUTURE_SESSION,
        }
        return RecoveryActionCandidate(
            id=candidate_id,
            action_type=action,
            target_session_id=target,
            target_week_start=week,  # type: ignore[arg-type]
            redesign_goal=goal,
            evidence_pattern_ids=evidence,
            impact_snapshot_id=impact.id,
            requires_schedule_draft=schedule,
            requires_session_design_draft=redesign,
            requires_plan_revision=revision,
            requires_calendar_reconciliation=(
                revision and target in set(impact.calendar_bound_session_ids)
            ),
            deterministic_rank=rank,
        )
