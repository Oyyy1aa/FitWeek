"""Deterministic selection from the reviewed exercise catalog."""

from __future__ import annotations

from datetime import datetime

from app.domain.common import LocationType
from app.domain.exercises.models import (
    Exercise,
    ExerciseDifficulty,
    ExerciseStatus,
)
from app.domain.planning.models import GenerationFailureReason, PlanGenerationError
from app.domain.planning.policies import GenerationPolicy
from app.domain.profiles.models import FitnessGoal, FitnessProfile, UserConstraint
from app.domain.sessions.models import SessionType
from app.planning.context_preferences import PlanningContextPreferences
from app.safety.constraint_validator import ConstraintValidator

_STRENGTH_PATTERNS = frozenset(
    {
        "squat",
        "hinge",
        "horizontal_push",
        "horizontal_pull",
        "vertical_push",
        "core_stability",
        "calf_raise",
    }
)


class ExerciseSelector:
    """Pre-filter hard violations, then rank candidates with stable keys."""

    def __init__(self, validator: ConstraintValidator | None = None) -> None:
        self._constraints = validator or ConstraintValidator()

    def select(
        self,
        *,
        profile: FitnessProfile,
        constraints: tuple[UserConstraint, ...],
        catalog: tuple[Exercise, ...],
        location: LocationType,
        session_type: SessionType,
        at: datetime,
        session_index: int,
        policy: GenerationPolicy,
        context_preferences: PlanningContextPreferences | None = None,
        variant: int = 0,
    ) -> tuple[Exercise, ...]:
        available_equipment = self._constraints.available_equipment(constraints, at=at)
        excluded_features = self._constraints.excluded_features(constraints, at=at)
        eligible = [
            exercise
            for exercise in catalog
            if self._eligible(
                exercise,
                profile=profile,
                location=location,
                available_equipment=available_equipment,
                excluded_features=excluded_features,
            )
        ]
        if not eligible:
            raise PlanGenerationError(
                GenerationFailureReason(
                    code="NO_ELIGIBLE_EXERCISES",
                    message="No controlled exercise satisfies the hard constraints.",
                )
            )
        if profile.primary_goal is FitnessGoal.BASIC_STRENGTH and not any(
            set(exercise.movement_patterns) & _STRENGTH_PATTERNS
            for exercise in eligible
        ):
            raise PlanGenerationError(
                GenerationFailureReason(
                    code="NO_ELIGIBLE_EXERCISES",
                    message=(
                        "No eligible basic-strength exercise is available with the "
                        "declared equipment."
                    ),
                )
            )

        ordered = sorted(
            eligible,
            key=lambda exercise: self._rank_key(
                exercise,
                goal=profile.primary_goal,
                session_type=session_type,
                available_equipment=available_equipment,
                preferences=context_preferences,
            ),
        )
        target_count = min(policy.exercises_per_session, len(ordered))
        if target_count < 3:
            raise PlanGenerationError(
                GenerationFailureReason(
                    code="INSUFFICIENT_EXERCISE_VARIETY",
                    message="At least three distinct eligible exercises are required.",
                )
            )

        rotation = (session_index + variant) % len(ordered)
        rotated = ordered[rotation:] + ordered[:rotation]
        warmups = [item for item in rotated if self._is_warmup(item)]
        cooldowns = [item for item in rotated if self._is_cooldown(item)]
        selected: list[Exercise] = []
        self._append_first_distinct(selected, warmups)

        main_count = target_count - 2
        for exercise in rotated:
            if exercise in selected or self._is_cooldown(exercise):
                continue
            selected.append(exercise)
            if len(selected) >= 1 + main_count:
                break

        self._append_first_distinct(selected, cooldowns)
        for exercise in rotated:
            if exercise not in selected:
                selected.append(exercise)
            if len(selected) == target_count:
                break

        selected = selected[:target_count]
        if len(selected) < 3:
            raise PlanGenerationError(
                GenerationFailureReason(
                    code="INSUFFICIENT_EXERCISE_VARIETY",
                    message="A safe session needs three distinct exercises.",
                )
            )
        return tuple(selected)

    @staticmethod
    def _eligible(
        exercise: Exercise,
        *,
        profile: FitnessProfile,
        location: LocationType,
        available_equipment: frozenset[str],
        excluded_features: frozenset[str],
    ) -> bool:
        if exercise.status is not ExerciseStatus.ACTIVE:
            return False
        if location not in exercise.location_types:
            return False
        required = frozenset(item.casefold() for item in exercise.required_equipment)
        if not required.issubset(available_equipment):
            return False
        tags = frozenset(item.casefold() for item in exercise.feature_tags)
        if tags & excluded_features:
            return False
        if (
            profile.experience_level.value == "BEGINNER"
            and exercise.difficulty_level is not ExerciseDifficulty.BEGINNER
        ):
            return False
        return not (
            profile.primary_goal is FitnessGoal.LOW_IMPACT_CARDIO
            and tags & {"jumping", "high_impact", "running"}
        )

    @classmethod
    def _rank_key(
        cls,
        exercise: Exercise,
        *,
        goal: FitnessGoal,
        session_type: SessionType,
        available_equipment: frozenset[str],
        preferences: PlanningContextPreferences | None,
    ) -> tuple[int, int, int, int, str]:
        score = cls._goal_score(exercise, goal=goal, session_type=session_type)
        if goal is FitnessGoal.BASIC_STRENGTH and exercise.required_equipment:
            if set(exercise.required_equipment).issubset(available_equipment):
                score += 2
        difficulty = (
            0 if exercise.difficulty_level is ExerciseDifficulty.BEGINNER else 1
        )
        values = {
            exercise.id.casefold(),
            exercise.name.casefold(),
            *(item.casefold() for item in exercise.feature_tags),
            *(item.casefold() for item in exercise.movement_patterns),
        }
        disliked = {
            item.casefold()
            for item in (preferences.disliked_activities if preferences else ())
        }
        preferred_equipment = {
            item.casefold()
            for item in (preferences.preferred_equipment if preferences else ())
        }
        disliked_rank = 1 if values & disliked else 0
        equipment_rank = (
            0
            if preferred_equipment
            and {item.casefold() for item in exercise.required_equipment}
            & preferred_equipment
            else 1
        )
        return (-score, disliked_rank, equipment_rank, difficulty, exercise.id)

    @staticmethod
    def _goal_score(
        exercise: Exercise, *, goal: FitnessGoal, session_type: SessionType
    ) -> int:
        patterns = set(exercise.movement_patterns)
        tags = set(exercise.feature_tags)
        strength = bool(patterns & _STRENGTH_PATTERNS)
        mobility = bool(patterns & {"mobility", "stretch"})
        cardio = "low_impact" in tags or "locomotion" in patterns
        if goal is FitnessGoal.BASIC_STRENGTH:
            return 8 if strength else 2 if mobility else 1
        if goal is FitnessGoal.LOW_IMPACT_CARDIO:
            return 8 if cardio else 3 if mobility else 1
        if goal is FitnessGoal.MOBILITY:
            return 8 if mobility else 2
        if goal is FitnessGoal.BUILD_HABIT:
            return 6 if tags & {"bodyweight", "low_impact"} else 3
        if goal is FitnessGoal.MIXED or session_type is SessionType.MIXED:
            return 5 if strength or mobility or cardio else 2
        return 5 if strength else 4 if cardio else 3 if mobility else 1

    @staticmethod
    def _is_warmup(exercise: Exercise) -> bool:
        return bool(
            set(exercise.movement_patterns) & {"mobility", "locomotion"}
            or "low_impact" in exercise.feature_tags
        )

    @staticmethod
    def _is_cooldown(exercise: Exercise) -> bool:
        return bool(set(exercise.movement_patterns) & {"stretch", "mobility"})

    @staticmethod
    def _append_first_distinct(
        selected: list[Exercise], candidates: list[Exercise]
    ) -> None:
        for exercise in candidates:
            if exercise not in selected:
                selected.append(exercise)
                return
