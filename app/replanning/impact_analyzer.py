"""Classify immutable, preserved, and affected sessions without side effects."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from app.domain.checkins.models import WorkoutCheckIn
from app.domain.exercises.models import Exercise
from app.domain.plans.models import WeeklyPlan
from app.domain.replanning.models import (
    ChangeImpact,
    ChangeImpactReason,
    LocalReplanCommand,
    PlanChangeType,
)
from app.domain.sessions.models import WorkoutSession


class ChangeImpactAnalyzer:
    def analyze(
        self,
        *,
        plan: WeeklyPlan,
        check_ins: Sequence[WorkoutCheckIn],
        change: LocalReplanCommand,
        exercise_catalog: Mapping[str, Exercise],
    ) -> ChangeImpact:
        checked_session_ids = {item.session_id for item in check_ins}
        affected: list[UUID] = []
        preserved: list[UUID] = []
        immutable: list[UUID] = []
        reasons: list[ChangeImpactReason] = []

        for session in plan.sessions:
            is_before_effective = session.scheduled_start < change.effective_from
            is_checked = session.id in checked_session_ids
            is_immutable = is_checked or is_before_effective
            impact_code = self._impact_code(
                session=session,
                change=change,
                exercise_catalog=exercise_catalog,
            )
            if is_immutable:
                immutable.append(session.id)
                if is_checked and not is_before_effective and impact_code is not None:
                    reasons.append(
                        ChangeImpactReason(
                            code="IMMUTABLE_SESSION_CONFLICT",
                            message=(
                                "The requested change would modify an immutable "
                                "session."
                            ),
                            session_id=session.id,
                        )
                    )
                continue
            if impact_code is None:
                preserved.append(session.id)
                continue
            affected.append(session.id)
            reasons.append(
                ChangeImpactReason(
                    code=impact_code,
                    message="The future session no longer satisfies the change.",
                    session_id=session.id,
                )
            )

        return ChangeImpact(
            affected_session_ids=tuple(affected),
            preserved_session_ids=tuple(preserved),
            immutable_session_ids=tuple(immutable),
            reasons=tuple(reasons),
        )

    @staticmethod
    def _impact_code(
        *,
        session: WorkoutSession,
        change: LocalReplanCommand,
        exercise_catalog: Mapping[str, Exercise],
    ) -> str | None:
        if change.change_type is PlanChangeType.AVAILABILITY_CHANGED:
            fits = any(
                slot.location_type is session.location_type
                and slot.start_utc <= session.scheduled_start
                and slot.end_utc >= session.scheduled_end
                for slot in change.replacement_availability_slots
            )
            return None if fits else "AVAILABILITY_CONFLICT"
        if change.change_type is PlanChangeType.SESSION_DURATION_CHANGED:
            return (
                "SESSION_DURATION_CONFLICT"
                if session.estimated_minutes > (change.max_session_minutes or 60)
                else None
            )
        if change.change_type is PlanChangeType.EQUIPMENT_CHANGED:
            available = frozenset(change.available_equipment or ())
            return (
                "EQUIPMENT_CONFLICT"
                if any(
                    not exercise_catalog[item.exercise_id].required_equipment.issubset(
                        available
                    )
                    for item in session.exercises
                    if item.exercise_id in exercise_catalog
                )
                else None
            )
        excluded = frozenset(change.excluded_features or ())
        return (
            "EXCLUDED_FEATURE_CONFLICT"
            if any(
                exercise_catalog[item.exercise_id].feature_tags & excluded
                for item in session.exercises
                if item.exercise_id in exercise_catalog
            )
            else None
        )
