"""Build immutable workout sessions from selected slots and exercises."""

from datetime import timedelta
from uuid import UUID, uuid5

from app.domain.exercises.models import Exercise
from app.domain.profiles.models import ExperienceLevel, FitnessProfile
from app.domain.sessions.models import (
    SessionExercise,
    SessionType,
    WorkoutSession,
    WorkoutSessionStatus,
)
from app.planning.slot_selector import SelectedSlot


class SessionBuilder:
    """Apply small, reviewed prescriptions without medical claims."""

    def build(
        self,
        *,
        plan_id: UUID,
        session_index: int,
        slot: SelectedSlot,
        session_type: SessionType,
        exercises: tuple[Exercise, ...],
        profile: FitnessProfile,
    ) -> WorkoutSession:
        session_id = uuid5(plan_id, f"session:{session_index}")
        prescriptions = tuple(
            self._prescribe(
                exercise,
                sequence_no=index,
                is_warmup=index == 1,
                is_cooldown=index == len(exercises),
                experience=profile.experience_level,
            )
            for index, exercise in enumerate(exercises, start=1)
        )
        estimated_minutes = slot.estimated_minutes
        return WorkoutSession(
            id=session_id,
            plan_id=plan_id,
            scheduled_start=slot.start,
            scheduled_end=slot.start + timedelta(minutes=estimated_minutes),
            location_type=slot.location_type,
            session_type=session_type,
            estimated_minutes=estimated_minutes,
            target_difficulty=(
                3 if profile.experience_level is ExperienceLevel.BEGINNER else 5
            ),
            status=WorkoutSessionStatus.PLANNED,
            exercises=prescriptions,
            version=1,
        )

    @staticmethod
    def _prescribe(
        exercise: Exercise,
        *,
        sequence_no: int,
        is_warmup: bool,
        is_cooldown: bool,
        experience: ExperienceLevel,
    ) -> SessionExercise:
        if is_warmup or is_cooldown:
            limit = 180 if is_warmup else 120
            return SessionExercise(
                exercise_id=exercise.id,
                sequence_no=sequence_no,
                sets=None,
                repetitions=None,
                duration_seconds=min(exercise.default_duration_seconds, limit),
                rest_seconds=15 if is_warmup else 0,
            )
        return SessionExercise(
            exercise_id=exercise.id,
            sequence_no=sequence_no,
            sets=2,
            repetitions=8 if experience is ExperienceLevel.BEGINNER else 10,
            duration_seconds=None,
            rest_seconds=30,
        )
