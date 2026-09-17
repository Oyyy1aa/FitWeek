"""Deterministically choose usable workout intervals from user availability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.domain.common import LocationType
from app.domain.planning.models import (
    GenerateWeeklyPlanCommand,
    GenerationFailureReason,
    PlanGenerationError,
)
from app.domain.planning.policies import GenerationPolicy
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.planning.context_preferences import PlanningContextPreferences
from app.safety.constraint_validator import ConstraintValidator


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectedSlot:
    start: datetime
    end: datetime
    location_type: LocationType

    @property
    def estimated_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


class SlotSelector:
    """Apply hard time/location constraints before a candidate is constructed."""

    def __init__(self, validator: ConstraintValidator | None = None) -> None:
        self._constraints = validator or ConstraintValidator()

    def select(
        self,
        *,
        profile: FitnessProfile,
        constraints: tuple[UserConstraint, ...],
        command: GenerateWeeklyPlanCommand,
        policy: GenerationPolicy,
        context_preferences: PlanningContextPreferences | None = None,
        variant: int = 0,
    ) -> tuple[SelectedSlot, ...]:
        candidates: list[SelectedSlot] = []
        rejected_by_unavailable = 0
        allowed_location_values = {location.value for location in LocationType}
        memory_locations = tuple(
            LocationType(normalized)
            for item in (
                context_preferences.preferred_locations if context_preferences else ()
            )
            if (normalized := item.strip().upper()) in allowed_location_values
        )
        effective_locations = command.preferred_locations or memory_locations
        preference_order = {
            location: index for index, location in enumerate(effective_locations)
        }
        preferred_times = set(
            context_preferences.preferred_times_of_day if context_preferences else ()
        )

        for slot in command.availability_slots:
            start = slot.start_utc
            end = slot.end_utc
            allowed = self._constraints.allowed_locations(constraints, at=start)
            if (
                allowed is not None
                and slot.location_type.value.casefold() not in allowed
            ):
                continue
            maximum = min(
                60,
                self._constraints.effective_max_session_minutes(
                    profile_limit=profile.max_session_minutes,
                    constraints=constraints,
                    at=start,
                ),
            )
            target = min(policy.target_session_minutes, maximum)
            intervals = self._available_intervals(
                start,
                end,
                self._constraints.unavailable_intervals(constraints, at=start),
            )
            selected_interval = self._first_usable_interval(
                intervals,
                target_minutes=target,
                minimum_minutes=policy.minimum_session_minutes,
            )
            if selected_interval is None:
                if intervals != ((start, end),):
                    rejected_by_unavailable += 1
                continue
            candidate_start, candidate_end = selected_interval
            candidates.append(
                SelectedSlot(
                    start=candidate_start,
                    end=candidate_end,
                    location_type=slot.location_type,
                )
            )

        candidates.sort(
            key=lambda item: (
                preference_order.get(item.location_type, len(preference_order)),
                self._time_preference_rank(item.start, preferred_times),
                item.start,
                str(item.location_type),
            )
        )
        if variant and candidates:
            offset = variant % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]

        selected = self._spread_across_days(candidates, profile.weekly_frequency)
        if len(selected) < profile.weekly_frequency:
            reasons = [
                GenerationFailureReason(
                    code="INSUFFICIENT_AVAILABILITY",
                    message=(
                        f"Only {len(selected)} valid slots are available, but "
                        f"{profile.weekly_frequency} are required."
                    ),
                )
            ]
            if not candidates:
                reasons.append(
                    GenerationFailureReason(
                        code="NO_VALID_TIME_SLOT",
                        message="No availability slot satisfies the planning policy.",
                    )
                )
            if rejected_by_unavailable:
                reasons.append(
                    GenerationFailureReason(
                        code="UNAVAILABLE_TIME_CONFLICT",
                        message=(
                            "One or more slots conflict with a hard unavailable-time "
                            "constraint."
                        ),
                    )
                )
            raise PlanGenerationError(*reasons)
        return tuple(sorted(selected, key=lambda item: item.start))

    @staticmethod
    def _time_preference_rank(value: datetime, preferences: set[str]) -> int:
        if not preferences:
            return 0
        hour = value.hour
        period = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        return 0 if period in preferences else 1

    @staticmethod
    def _available_intervals(
        start: datetime,
        end: datetime,
        blocked: tuple[tuple[datetime, datetime], ...],
    ) -> tuple[tuple[datetime, datetime], ...]:
        intervals = [(start, end)]
        for blocked_start, blocked_end in sorted(blocked):
            blocked_start = blocked_start.astimezone(UTC)
            blocked_end = blocked_end.astimezone(UTC)
            remaining: list[tuple[datetime, datetime]] = []
            for current_start, current_end in intervals:
                if blocked_end <= current_start or blocked_start >= current_end:
                    remaining.append((current_start, current_end))
                    continue
                if current_start < blocked_start:
                    remaining.append((current_start, min(blocked_start, current_end)))
                if blocked_end < current_end:
                    remaining.append((max(blocked_end, current_start), current_end))
            intervals = remaining
        return tuple(intervals)

    @staticmethod
    def _first_usable_interval(
        intervals: tuple[tuple[datetime, datetime], ...],
        *,
        target_minutes: int,
        minimum_minutes: int,
    ) -> tuple[datetime, datetime] | None:
        for start, end in intervals:
            available_minutes = int((end - start).total_seconds() // 60)
            duration = min(target_minutes, available_minutes)
            if duration >= minimum_minutes:
                return start, start + timedelta(minutes=duration)
        return None

    @staticmethod
    def _spread_across_days(
        candidates: list[SelectedSlot], required: int
    ) -> list[SelectedSlot]:
        selected: list[SelectedSlot] = []
        used_days = set()
        for candidate in candidates:
            day = candidate.start.date()
            if day in used_days:
                continue
            selected.append(candidate)
            used_days.add(day)
            if len(selected) == required:
                return selected
        selected_ids = {(item.start, item.end, item.location_type) for item in selected}
        for candidate in candidates:
            key = (candidate.start, candidate.end, candidate.location_type)
            if key in selected_ids:
                continue
            selected.append(candidate)
            if len(selected) == required:
                break
        return selected
