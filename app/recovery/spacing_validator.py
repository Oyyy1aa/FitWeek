"""Non-medical read-only weekly spacing and frequency precheck."""

from collections import Counter
from uuid import UUID

from app.domain.plans.models import WeeklyPlan
from app.domain.recovery.enums import RecoveryActionType
from app.domain.recovery.models import (
    RecoveryActionCandidate,
    RecoverySpacingValidation,
)


class RecoverySpacingValidator:
    def validate(
        self,
        *,
        plan: WeeklyPlan,
        selected: tuple[RecoveryActionCandidate, ...],
        minimum_frequency: int = 2,
        maximum_frequency: int = 5,
    ) -> RecoverySpacingValidation:
        removed = {
            item.target_session_id
            for item in selected
            if item.action_type is RecoveryActionType.REMOVE_FUTURE_SESSION
        }
        remaining = tuple(item for item in plan.sessions if item.id not in removed)
        violations: list[str] = []
        affected: set[UUID] = {item for item in removed if item is not None}
        if not minimum_frequency <= len(remaining) <= maximum_frequency:
            violations.append("RECOVERY_FREQUENCY_VIOLATION")
        by_day = Counter(item.scheduled_start.date() for item in remaining)
        duplicate_days = {day for day, count in by_day.items() if count > 1}
        if duplicate_days:
            violations.append("RECOVERY_SPACING_VIOLATION")
            affected.update(
                item.id
                for item in remaining
                if item.scheduled_start.date() in duplicate_days
            )
        ordered = sorted(remaining, key=lambda item: item.scheduled_start)
        consecutive_days = 1
        for left, right in zip(ordered, ordered[1:], strict=False):
            gap_seconds = (right.scheduled_start - left.scheduled_end).total_seconds()
            if gap_seconds < 12 * 3600:
                violations.append("RECOVERY_SPACING_VIOLATION")
                affected.update((left.id, right.id))
            if left.target_difficulty >= 8 and right.target_difficulty >= 8:
                if gap_seconds < 24 * 3600:
                    violations.append("RECOVERY_HIGH_LOAD_SPACING_VIOLATION")
                    affected.update((left.id, right.id))
            day_delta = (
                right.scheduled_start.date() - left.scheduled_start.date()
            ).days
            consecutive_days = consecutive_days + 1 if day_delta == 1 else 1
            if consecutive_days >= 3:
                violations.append("RECOVERY_CONSECUTIVE_DAYS_VIOLATION")
                affected.update((left.id, right.id))
        codes = tuple(sorted(set(violations)))
        return RecoverySpacingValidation(
            passed=not codes,
            violation_codes=codes,
            affected_session_ids=tuple(sorted(affected, key=str)),
        )
