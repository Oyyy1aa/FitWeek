"""Versioned deterministic duration calculation."""

from app.domain.session_design.models import SessionDurationBreakdown
from app.domain.sessions.models import SessionExercise


class SessionDurationPolicy:
    version = "session-duration-policy-v1"
    transition_seconds = 15

    def calculate(
        self, exercises: tuple[SessionExercise, ...]
    ) -> SessionDurationBreakdown:
        exercise_seconds = sum(
            item.duration_seconds
            if item.duration_seconds is not None
            else (item.sets or 1) * (item.repetitions or 1) * 3
            for item in exercises
        )
        rest_seconds = sum(item.rest_seconds for item in exercises)
        transition_seconds = max(0, len(exercises) - 1) * self.transition_seconds
        return SessionDurationBreakdown(
            exercise_seconds=exercise_seconds,
            rest_seconds=rest_seconds,
            transition_seconds=transition_seconds,
            total_seconds=exercise_seconds + rest_seconds + transition_seconds,
            policy_version=self.version,
        )

    def fit_exact(
        self, exercises: tuple[SessionExercise, ...], target_minutes: int
    ) -> tuple[tuple[SessionExercise, ...], SessionDurationBreakdown]:
        target = target_minutes * 60
        transitions = max(0, len(exercises) - 1) * self.transition_seconds
        rest = sum(item.rest_seconds for item in exercises)
        remaining = target - transitions - rest
        if remaining < len(exercises):
            raise ValueError("target duration cannot contain the selected exercises")
        base, extra = divmod(remaining, len(exercises))
        fitted = tuple(
            SessionExercise(
                exercise_id=item.exercise_id,
                sequence_no=index,
                sets=None,
                repetitions=None,
                duration_seconds=base + (1 if index <= extra else 0),
                rest_seconds=item.rest_seconds,
            )
            for index, item in enumerate(exercises, start=1)
        )
        return fitted, self.calculate(fitted)
