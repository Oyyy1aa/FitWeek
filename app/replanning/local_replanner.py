"""Rebuild only sessions identified by deterministic change impact analysis."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from app.domain.common import LocationType
from app.domain.exercises.models import Exercise, ExerciseDifficulty, ExerciseStatus
from app.domain.planning.models import AvailabilitySlot
from app.domain.plans.models import WeeklyPlan
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.domain.replanning.models import (
    ChangeImpact,
    LocalReplanCommand,
    LocalReplanningError,
    PlanChangeType,
    ReplanningFailureReason,
)
from app.domain.sessions.models import SessionExercise, WorkoutSession
from app.safety.constraint_validator import ConstraintValidator


class LocalReplanner:
    """A bounded, reproducible transformer with no persistence or HTTP knowledge."""

    def __init__(self, validator: ConstraintValidator | None = None) -> None:
        self._constraints = validator or ConstraintValidator()

    def rebuild(
        self,
        *,
        source: WeeklyPlan,
        profile: FitnessProfile,
        constraints: Sequence[UserConstraint],
        catalog: Mapping[str, Exercise],
        change: LocalReplanCommand,
        impact: ChangeImpact,
        fingerprint: str,
        variant: int = 0,
    ) -> tuple[WorkoutSession, ...]:
        affected = set(impact.affected_session_ids)
        frozen = set(impact.immutable_session_ids) | set(impact.preserved_session_ids)
        occupied = [
            (item.scheduled_start, item.scheduled_end)
            for item in source.sessions
            if item.id in frozen
        ]
        slots = sorted(
            change.replacement_availability_slots,
            key=lambda item: (item.start_utc, item.end_utc, item.location_type.value),
        )
        rebuilt: list[WorkoutSession] = []
        for index, session in enumerate(source.sessions):
            if session.id not in affected:
                rebuilt.append(session)
                continue
            if change.change_type is PlanChangeType.AVAILABILITY_CHANGED:
                replacement = self._move_session(
                    session=session,
                    slots=slots,
                    occupied=occupied,
                    profile=profile,
                    constraints=constraints,
                    catalog=catalog,
                    namespace=source.series_id,
                    fingerprint=fingerprint,
                    session_index=index,
                    variant=variant,
                )
                occupied.append(
                    (replacement.scheduled_start, replacement.scheduled_end)
                )
            elif change.change_type is PlanChangeType.SESSION_DURATION_CHANGED:
                replacement = self._shorten_session(
                    session,
                    maximum=change.max_session_minutes or 60,
                    namespace=source.series_id,
                    fingerprint=fingerprint,
                )
            else:
                replacement = self._replace_unsafe_exercises(
                    session=session,
                    profile=profile,
                    constraints=constraints,
                    catalog=catalog,
                    namespace=source.series_id,
                    fingerprint=fingerprint,
                    variant=variant,
                )
            rebuilt.append(replacement)
        return tuple(rebuilt)

    def _move_session(
        self,
        *,
        session: WorkoutSession,
        slots: list[AvailabilitySlot],
        occupied: list[tuple[datetime, datetime]],
        profile: FitnessProfile,
        constraints: Sequence[UserConstraint],
        catalog: Mapping[str, Exercise],
        namespace: UUID,
        fingerprint: str,
        session_index: int,
        variant: int,
    ) -> WorkoutSession:
        minutes = min(session.estimated_minutes, profile.max_session_minutes)
        choices = [
            slot
            for slot in slots
            if (slot.end_utc - slot.start_utc) >= timedelta(minutes=minutes)
            and not any(
                slot.start_utc < end
                and slot.start_utc + timedelta(minutes=minutes) > start
                for start, end in occupied
            )
        ]
        if not choices:
            raise LocalReplanningError(
                ReplanningFailureReason(
                    code="INSUFFICIENT_REPLACEMENT_SLOTS",
                    message="No replacement slot is available for an affected session.",
                    session_id=session.id,
                )
            )
        slot = choices[min(variant, len(choices) - 1)]
        candidate = replace(
            session,
            id=uuid5(namespace, f"{fingerprint}:session:{session_index}:{variant}"),
            scheduled_start=slot.start_utc,
            scheduled_end=slot.start_utc + timedelta(minutes=minutes),
            location_type=slot.location_type,
            estimated_minutes=minutes,
            version=1,
        )
        return self._replace_unsafe_exercises(
            session=candidate,
            profile=profile,
            constraints=constraints,
            catalog=catalog,
            namespace=namespace,
            fingerprint=fingerprint,
            variant=variant,
            preserve_id=True,
        )

    @staticmethod
    def _shorten_session(
        session: WorkoutSession,
        *,
        maximum: int,
        namespace: UUID,
        fingerprint: str,
    ) -> WorkoutSession:
        exercises = session.exercises
        if len(exercises) > 3:
            exercises = exercises[: max(3, len(exercises) - 1)]
        exercises = tuple(
            replace(item, sequence_no=index)
            for index, item in enumerate(exercises, start=1)
        )
        return replace(
            session,
            id=uuid5(namespace, f"{fingerprint}:duration:{session.id}"),
            scheduled_end=session.scheduled_start + timedelta(minutes=maximum),
            estimated_minutes=maximum,
            exercises=exercises,
            version=1,
        )

    def _replace_unsafe_exercises(
        self,
        *,
        session: WorkoutSession,
        profile: FitnessProfile,
        constraints: Sequence[UserConstraint],
        catalog: Mapping[str, Exercise],
        namespace: UUID,
        fingerprint: str,
        variant: int,
        preserve_id: bool = False,
    ) -> WorkoutSession:
        available = self._constraints.available_equipment(
            constraints, at=session.scheduled_start
        )
        excluded = self._constraints.excluded_features(
            constraints, at=session.scheduled_start
        )
        used: set[str] = set()
        prescriptions: list[SessionExercise] = []
        changed = False
        for item in session.exercises:
            exercise = catalog.get(item.exercise_id)
            if (
                exercise is not None
                and self._eligible(
                    exercise,
                    profile=profile,
                    location=session.location_type,
                    available=available,
                    excluded=excluded,
                )
                and exercise.id not in used
            ):
                replacement = exercise
            else:
                replacement = self._find_replacement(
                    original=exercise,
                    profile=profile,
                    location=session.location_type,
                    available=available,
                    excluded=excluded,
                    catalog=catalog,
                    used=used,
                    variant=variant,
                )
                changed = True
            used.add(replacement.id)
            prescriptions.append(replace(item, exercise_id=replacement.id))
        if not changed and preserve_id:
            return session
        return replace(
            session,
            id=(
                session.id
                if preserve_id
                else uuid5(namespace, f"{fingerprint}:exercise:{session.id}")
            ),
            exercises=tuple(prescriptions),
            version=1,
        )

    def _find_replacement(
        self,
        *,
        original: Exercise | None,
        profile: FitnessProfile,
        location: LocationType,
        available: frozenset[str],
        excluded: frozenset[str],
        catalog: Mapping[str, Exercise],
        used: set[str],
        variant: int,
    ) -> Exercise:
        candidates = [
            item
            for item in catalog.values()
            if item.id not in used
            and self._eligible(
                item,
                profile=profile,
                location=location,
                available=available,
                excluded=excluded,
            )
        ]
        candidates.sort(
            key=lambda item: (
                -len(
                    item.movement_patterns
                    & (original.movement_patterns if original else frozenset())
                ),
                len(item.required_equipment),
                item.difficulty_level.value,
                item.id,
            )
        )
        if not candidates:
            raise LocalReplanningError(
                ReplanningFailureReason(
                    code="NO_SAFE_REPLACEMENT_EXERCISE",
                    message="No safe controlled-catalog replacement is available.",
                )
            )
        return candidates[min(variant, len(candidates) - 1)]

    @staticmethod
    def _eligible(
        exercise: Exercise,
        *,
        profile: FitnessProfile,
        location: LocationType,
        available: frozenset[str],
        excluded: frozenset[str],
    ) -> bool:
        return (
            exercise.status is ExerciseStatus.ACTIVE
            and location in exercise.location_types
            and exercise.required_equipment.issubset(available)
            and not exercise.feature_tags & excluded
            and (
                profile.experience_level.value != "BEGINNER"
                or exercise.difficulty_level is ExerciseDifficulty.BEGINNER
            )
        )
