"""Deterministic, side-effect-free weekly plan safety validation."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from app.safety.constraint_validator import ConstraintValidator
from app.safety.models import SafetyValidationResult, SafetyViolation
from app.safety.scope_guard import ScopeGuard

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from app.domain.exercises.models import Exercise
    from app.domain.plans.models import WeeklyPlan
    from app.domain.profiles.models import FitnessProfile, UserConstraint
    from app.domain.sessions.models import WorkoutSession


def _enum_token(value: object) -> str:
    return str(getattr(value, "value", value)).strip().casefold()


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


class SafetyEngine:
    """Validate all Phase 1A hard rules without mutating inputs or doing I/O."""

    def __init__(
        self,
        *,
        scope_guard: ScopeGuard | None = None,
        constraint_validator: ConstraintValidator | None = None,
    ) -> None:
        self._scope_guard = scope_guard or ScopeGuard()
        self._constraints = constraint_validator or ConstraintValidator()

    def validate_plan(
        self,
        *,
        profile: FitnessProfile,
        constraints: Sequence[UserConstraint],
        plan: WeeklyPlan,
        exercise_catalog: Mapping[str, Exercise],
    ) -> SafetyValidationResult:
        """Return every detectable violation in a stable traversal order."""

        violations = list(self._scope_guard.validate(profile))

        if len(plan.sessions) != profile.weekly_frequency:
            violations.append(
                SafetyViolation(
                    code="WEEKLY_FREQUENCY_MISMATCH",
                    message=(
                        "Session count does not match the configured weekly frequency."
                    ),
                    path="sessions",
                )
            )

        week_start = datetime.combine(plan.week_start, time.min, tzinfo=UTC)
        week_end = week_start + timedelta(days=7)
        valid_session_times: list[tuple[int, WorkoutSession, datetime, datetime]] = []

        for session_index, session in enumerate(plan.sessions):
            session_path = f"sessions[{session_index}]"
            start_is_aware = _is_aware(session.scheduled_start)
            end_is_aware = _is_aware(session.scheduled_end)

            if not start_is_aware or not end_is_aware:
                violations.append(
                    SafetyViolation(
                        code="NAIVE_DATETIME",
                        message=(
                            "Session timestamps must be timezone-aware UTC datetimes."
                        ),
                        path=f"{session_path}.scheduled_start",
                        session_id=session.id,
                    )
                )

            if (
                start_is_aware
                and end_is_aware
                and (
                    session.scheduled_start.utcoffset() != timedelta(0)
                    or session.scheduled_end.utcoffset() != timedelta(0)
                )
            ):
                violations.append(
                    SafetyViolation(
                        code="INVALID_SESSION_TIME",
                        message="Session timestamps must use UTC.",
                        path=f"{session_path}.scheduled_start",
                        session_id=session.id,
                    )
                )

            if (
                start_is_aware == end_is_aware
                and session.scheduled_start >= session.scheduled_end
            ):
                violations.append(
                    SafetyViolation(
                        code="INVALID_SESSION_TIME",
                        message="Session end time must be later than its start time.",
                        path=f"{session_path}.scheduled_end",
                        session_id=session.id,
                    )
                )

            if start_is_aware and end_is_aware:
                start_utc = session.scheduled_start.astimezone(UTC)
                end_utc = session.scheduled_end.astimezone(UTC)
                if start_utc < end_utc:
                    valid_session_times.append(
                        (session_index, session, start_utc, end_utc)
                    )
                if start_utc < week_start or end_utc > week_end:
                    violations.append(
                        SafetyViolation(
                            code="SESSION_OUTSIDE_WEEK",
                            message=(
                                "Session must be fully contained in the target week."
                            ),
                            path=f"{session_path}.scheduled_start",
                            session_id=session.id,
                        )
                    )

                unavailable_intervals = self._constraints.unavailable_intervals(
                    constraints,
                    at=start_utc,
                )
                if any(
                    start_utc < unavailable_end and end_utc > unavailable_start
                    for unavailable_start, unavailable_end in unavailable_intervals
                ):
                    violations.append(
                        SafetyViolation(
                            code="UNAVAILABLE_TIME_CONFLICT",
                            message=(
                                "Session overlaps a hard unavailable-time interval."
                            ),
                            path=f"{session_path}.scheduled_start",
                            session_id=session.id,
                        )
                    )

                duration_seconds = (end_utc - start_utc).total_seconds()
                expected_seconds = session.estimated_minutes * 60
                if abs(duration_seconds - expected_seconds) > 60:
                    violations.append(
                        SafetyViolation(
                            code="SESSION_DURATION_MISMATCH",
                            message=(
                                "Scheduled duration must match estimated minutes "
                                "within one minute."
                            ),
                            path=f"{session_path}.estimated_minutes",
                            session_id=session.id,
                        )
                    )

            constraint_time = session.scheduled_start if start_is_aware else week_start
            maximum_minutes = self._constraints.effective_max_session_minutes(
                profile_limit=profile.max_session_minutes,
                constraints=constraints,
                at=constraint_time,
            )
            if session.estimated_minutes < 15:
                violations.append(
                    SafetyViolation(
                        code="SESSION_TOO_SHORT",
                        message="Session duration must be at least 15 minutes.",
                        path=f"{session_path}.estimated_minutes",
                        session_id=session.id,
                    )
                )
            if session.estimated_minutes > min(60, maximum_minutes):
                violations.append(
                    SafetyViolation(
                        code="SESSION_TOO_LONG",
                        message="Session duration exceeds the applicable maximum.",
                        path=f"{session_path}.estimated_minutes",
                        session_id=session.id,
                    )
                )

            session_location = _enum_token(session.location_type)
            allowed_locations = self._constraints.allowed_locations(
                constraints,
                at=constraint_time,
            )
            if (
                allowed_locations is not None
                and session_location not in allowed_locations
            ):
                violations.append(
                    SafetyViolation(
                        code="LOCATION_MISMATCH",
                        message=(
                            "Session location is not permitted by a hard constraint."
                        ),
                        path=f"{session_path}.location_type",
                        session_id=session.id,
                    )
                )

            if not session.exercises:
                violations.append(
                    SafetyViolation(
                        code="EMPTY_SESSION",
                        message="A workout session must contain at least one exercise.",
                        path=f"{session_path}.exercises",
                        session_id=session.id,
                    )
                )

            available_equipment = self._constraints.available_equipment(
                constraints,
                at=constraint_time,
            )
            excluded_features = self._constraints.excluded_features(
                constraints,
                at=constraint_time,
            )
            seen_sequences: set[int] = set()

            for exercise_index, session_exercise in enumerate(session.exercises):
                exercise_path = f"{session_path}.exercises[{exercise_index}]"
                if session_exercise.sequence_no in seen_sequences:
                    violations.append(
                        SafetyViolation(
                            code="DUPLICATE_SEQUENCE",
                            message=(
                                "Exercise sequence numbers must be unique in a session."
                            ),
                            path=f"{exercise_path}.sequence_no",
                            session_id=session.id,
                            exercise_id=session_exercise.exercise_id,
                        )
                    )
                seen_sequences.add(session_exercise.sequence_no)

                exercise = exercise_catalog.get(session_exercise.exercise_id)
                if exercise is None:
                    violations.append(
                        SafetyViolation(
                            code="EXERCISE_NOT_FOUND",
                            message=(
                                "The referenced exercise is not in the "
                                "controlled catalog."
                            ),
                            path=f"{exercise_path}.exercise_id",
                            session_id=session.id,
                            exercise_id=session_exercise.exercise_id,
                        )
                    )
                    continue

                if _enum_token(exercise.status) == "disabled":
                    violations.append(
                        SafetyViolation(
                            code="EXERCISE_DISABLED",
                            message="The referenced exercise is disabled.",
                            path=f"{exercise_path}.exercise_id",
                            session_id=session.id,
                            exercise_id=exercise.id,
                        )
                    )

                supported_locations = {
                    _enum_token(location) for location in exercise.location_types
                }
                if session_location not in supported_locations:
                    violations.append(
                        SafetyViolation(
                            code="LOCATION_MISMATCH",
                            message="Exercise does not support the session location.",
                            path=f"{exercise_path}.exercise_id",
                            session_id=session.id,
                            exercise_id=exercise.id,
                        )
                    )

                required_equipment = {
                    equipment.strip().casefold()
                    for equipment in exercise.required_equipment
                    if equipment.strip()
                }
                if not required_equipment.issubset(available_equipment):
                    violations.append(
                        SafetyViolation(
                            code="EQUIPMENT_MISMATCH",
                            message="Required equipment is unavailable.",
                            path=exercise_path,
                            session_id=session.id,
                            exercise_id=exercise.id,
                        )
                    )

                feature_tags = {
                    feature.strip().casefold()
                    for feature in exercise.feature_tags
                    if feature.strip()
                }
                if feature_tags & excluded_features:
                    violations.append(
                        SafetyViolation(
                            code="EXCLUDED_FEATURE",
                            message=(
                                "Exercise contains a feature excluded by a "
                                "hard constraint."
                            ),
                            path=exercise_path,
                            session_id=session.id,
                            exercise_id=exercise.id,
                        )
                    )

        violations.extend(self._overlap_violations(valid_session_times))
        return SafetyValidationResult.from_violations(tuple(violations))

    def validate_session_design(
        self,
        *,
        profile: FitnessProfile,
        constraints: Sequence[UserConstraint],
        target_date: date,
        target_duration_minutes: int,
        location: object,
        exercises: Sequence[object],
        exercise_catalog: Mapping[str, Exercise],
    ) -> SafetyValidationResult:
        """Final independent gate for a review-only single Session Draft."""

        violations = list(self._scope_guard.validate(profile))
        at = datetime.combine(target_date, time.min, tzinfo=UTC)
        if target_duration_minutes < 15:
            violations.append(
                SafetyViolation(
                    code="SESSION_TOO_SHORT",
                    message="Session duration must be at least 15 minutes.",
                )
            )
        maximum = self._constraints.effective_max_session_minutes(
            profile_limit=profile.max_session_minutes,
            constraints=constraints,
            at=at,
        )
        if target_duration_minutes > min(60, maximum):
            violations.append(
                SafetyViolation(
                    code="SESSION_TOO_LONG",
                    message="Session duration exceeds the applicable maximum.",
                )
            )
        location_token = _enum_token(location)
        allowed_locations = self._constraints.allowed_locations(constraints, at=at)
        if allowed_locations is not None and location_token not in allowed_locations:
            violations.append(
                SafetyViolation(
                    code="LOCATION_MISMATCH",
                    message="Session location is not permitted by a hard constraint.",
                )
            )
        available_equipment = self._constraints.available_equipment(constraints, at=at)
        excluded_features = self._constraints.excluded_features(constraints, at=at)
        seen: set[int] = set()
        for index, selected in enumerate(exercises):
            exercise_id = str(getattr(selected, "exercise_id", ""))
            sequence_no = int(getattr(selected, "sequence_no", 0))
            path = f"exercises[{index}]"
            if sequence_no in seen:
                violations.append(
                    SafetyViolation(
                        code="DUPLICATE_SEQUENCE",
                        message="Exercise sequence numbers must be unique.",
                        path=f"{path}.sequence_no",
                        exercise_id=exercise_id,
                    )
                )
            seen.add(sequence_no)
            exercise = exercise_catalog.get(exercise_id)
            if exercise is None:
                violations.append(
                    SafetyViolation(
                        code="EXERCISE_NOT_FOUND",
                        message="Exercise is not in the controlled catalog.",
                        path=path,
                        exercise_id=exercise_id,
                    )
                )
                continue
            if _enum_token(exercise.status) == "disabled":
                violations.append(
                    SafetyViolation(
                        code="EXERCISE_DISABLED",
                        message="Exercise is disabled.",
                        path=path,
                        exercise_id=exercise_id,
                    )
                )
            if location_token not in {
                _enum_token(item) for item in exercise.location_types
            }:
                violations.append(
                    SafetyViolation(
                        code="LOCATION_MISMATCH",
                        message="Exercise does not support the requested location.",
                        path=path,
                        exercise_id=exercise_id,
                    )
                )
            if not {item.casefold() for item in exercise.required_equipment}.issubset(
                available_equipment
            ):
                violations.append(
                    SafetyViolation(
                        code="EQUIPMENT_MISMATCH",
                        message="Required equipment is unavailable.",
                        path=path,
                        exercise_id=exercise_id,
                    )
                )
            if {item.casefold() for item in exercise.feature_tags} & excluded_features:
                violations.append(
                    SafetyViolation(
                        code="EXCLUDED_FEATURE",
                        message="Exercise contains an excluded feature.",
                        path=path,
                        exercise_id=exercise_id,
                    )
                )
        if not exercises:
            violations.append(
                SafetyViolation(
                    code="EMPTY_SESSION",
                    message="A Session Draft must contain exercises.",
                )
            )
        return SafetyValidationResult.from_violations(tuple(violations))

    @staticmethod
    def _overlap_violations(
        sessions: list[tuple[int, WorkoutSession, datetime, datetime]],
    ) -> tuple[SafetyViolation, ...]:
        """Find overlaps without assuming the submitted sessions are pre-sorted."""

        if len(sessions) < 2:
            return ()

        ordered = sorted(sessions, key=lambda item: (item[2], item[3], str(item[1].id)))
        active_end = ordered[0][3]
        violations: list[SafetyViolation] = []
        for session_index, session, start, end in ordered[1:]:
            if start < active_end:
                violations.append(
                    SafetyViolation(
                        code="SESSION_OVERLAP",
                        message="Workout sessions in a weekly plan must not overlap.",
                        path=f"sessions[{session_index}].scheduled_start",
                        session_id=session.id,
                    )
                )
            if end > active_end:
                active_end = end
        return tuple(violations)
