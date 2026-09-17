"""Deterministic hard filtering and soft ordering for catalog candidates."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time

from app.domain.common import LocationType
from app.domain.exercises.models import Exercise, ExerciseDifficulty, ExerciseStatus
from app.domain.profiles.models import ExperienceLevel, FitnessProfile, UserConstraint
from app.domain.session_design.models import CandidateSlot
from app.domain.session_design.templates import SessionTemplate
from app.safety.constraint_validator import ConstraintValidator


class ExerciseCandidateSearcher:
    def __init__(self, constraints: ConstraintValidator | None = None) -> None:
        self._constraints = constraints or ConstraintValidator()

    def search(
        self,
        *,
        profile: FitnessProfile,
        constraints: Sequence[UserConstraint],
        catalog: Sequence[Exercise],
        template: SessionTemplate,
        location: LocationType,
        target_date: date,
    ) -> tuple[CandidateSlot, ...]:
        at = datetime.combine(target_date, time.min, tzinfo=UTC)
        equipment = self._constraints.available_equipment(constraints, at=at)
        excluded = self._constraints.excluded_features(constraints, at=at)
        eligible = [
            exercise
            for exercise in catalog
            if exercise.status is ExerciseStatus.ACTIVE
            and location in exercise.location_types
            and exercise.required_equipment <= equipment
            and not (exercise.feature_tags & excluded)
            and not (
                profile.experience_level is ExperienceLevel.BEGINNER
                and exercise.difficulty_level is ExerciseDifficulty.INTERMEDIATE
            )
            and exercise.movement_patterns
        ]
        result: list[CandidateSlot] = []
        for slot in template.slots:
            matches = [
                item
                for item in eligible
                if item.movement_patterns & slot.required_patterns
            ]
            matches.sort(
                key=lambda item: (
                    -len(item.feature_tags & slot.preferred_tags),
                    len(item.required_equipment),
                    item.difficulty_level.value,
                    item.id,
                )
            )
            result.append(
                CandidateSlot(
                    slot_id=slot.slot_id,
                    role=slot.role,
                    exercise_ids=tuple(sorted(item.id for item in matches)),
                )
            )
        return tuple(result)
