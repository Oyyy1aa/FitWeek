"""Pure helpers for interpreting structured user constraints."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.domain.profiles.models import UserConstraint


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _normalized_token(value: str) -> str:
    return value.strip().casefold()


class ConstraintValidator:
    """Interpret only the structured constraints supported by Phase 1A."""

    @staticmethod
    def _is_effective(constraint: UserConstraint, *, at: datetime) -> bool:
        valid_until = constraint.valid_until
        return valid_until is None or valid_until >= at

    def available_equipment(
        self,
        constraints: Sequence[UserConstraint],
        *,
        at: datetime,
    ) -> frozenset[str]:
        """Return equipment explicitly declared available at the session time."""

        return frozenset(
            _normalized_token(constraint.constraint_value)
            for constraint in constraints
            if _enum_value(constraint.constraint_type) == "AVAILABLE_EQUIPMENT"
            and self._is_effective(constraint, at=at)
            and constraint.constraint_value.strip()
        )

    def excluded_features(
        self,
        constraints: Sequence[UserConstraint],
        *,
        at: datetime,
    ) -> frozenset[str]:
        """Return active hard feature exclusions for a session."""

        return frozenset(
            _normalized_token(constraint.constraint_value)
            for constraint in constraints
            if _enum_value(constraint.constraint_type) == "EXCLUDED_FEATURE"
            and constraint.is_hard
            and self._is_effective(constraint, at=at)
            and constraint.constraint_value.strip()
        )

    def allowed_locations(
        self,
        constraints: Sequence[UserConstraint],
        *,
        at: datetime,
    ) -> frozenset[str] | None:
        """Return active hard location allow-list, or ``None`` when unrestricted."""

        locations = frozenset(
            _normalized_token(constraint.constraint_value)
            for constraint in constraints
            if _enum_value(constraint.constraint_type) == "ALLOWED_LOCATION"
            and constraint.is_hard
            and self._is_effective(constraint, at=at)
            and constraint.constraint_value.strip()
        )
        return locations or None

    def effective_max_session_minutes(
        self,
        *,
        profile_limit: int,
        constraints: Sequence[UserConstraint],
        at: datetime,
    ) -> int:
        """Apply the strictest active structured maximum to the profile limit."""

        limits = [profile_limit]
        for constraint in constraints:
            if _enum_value(constraint.constraint_type) != "MAX_SESSION_MINUTES" or not (
                constraint.is_hard and self._is_effective(constraint, at=at)
            ):
                continue
            try:
                value = int(constraint.constraint_value)
            except ValueError:
                continue
            if value > 0:
                limits.append(value)
        return min(limits)

    def unavailable_intervals(
        self,
        constraints: Sequence[UserConstraint],
        *,
        at: datetime,
    ) -> tuple[tuple[datetime, datetime], ...]:
        """Parse only validated hard ISO-8601 start/end interval constraints."""

        intervals: list[tuple[datetime, datetime]] = []
        for constraint in constraints:
            if _enum_value(constraint.constraint_type) != "UNAVAILABLE_TIME" or not (
                constraint.is_hard and self._is_effective(constraint, at=at)
            ):
                continue
            start_value, separator, end_value = constraint.constraint_value.partition(
                "/"
            )
            if not separator:
                continue
            try:
                start = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start.tzinfo is not None and end.tzinfo is not None and start < end:
                intervals.append((start, end))
        return tuple(intervals)
