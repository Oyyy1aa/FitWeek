"""Reviewed, deterministic MVP exercise catalog.

This module contains data only. Exercises cannot be created by the public API and
are never fetched from an external source.
"""

from app.domain.common import LocationType
from app.domain.exercises.models import (
    Exercise,
    ExerciseDifficulty,
    ExerciseStatus,
)


def _exercise(
    exercise_id: str,
    name: str,
    *,
    difficulty: ExerciseDifficulty = ExerciseDifficulty.BEGINNER,
    locations: tuple[LocationType, ...],
    equipment: tuple[str, ...] = (),
    patterns: tuple[str, ...],
    tags: tuple[str, ...] = (),
    duration: int = 45,
    status: ExerciseStatus = ExerciseStatus.ACTIVE,
) -> Exercise:
    return Exercise(
        id=exercise_id,
        name=name,
        difficulty_level=difficulty,
        location_types=frozenset(locations),
        required_equipment=frozenset(equipment),
        movement_patterns=frozenset(patterns),
        feature_tags=frozenset(tags),
        default_duration_seconds=duration,
        status=status,
        version=1,
    )


EXERCISE_CATALOG_SEED: tuple[Exercise, ...] = (
    _exercise(
        "bodyweight_squat",
        "Bodyweight Squat",
        locations=(LocationType.HOME, LocationType.GYM, LocationType.OUTDOOR),
        patterns=("squat",),
        tags=("standing", "bodyweight"),
    ),
    _exercise(
        "wall_push_up",
        "Wall Push-Up",
        locations=(LocationType.HOME, LocationType.GYM),
        patterns=("horizontal_push",),
        tags=("standing", "bodyweight"),
    ),
    _exercise(
        "chair_sit_to_stand",
        "Chair Sit-to-Stand",
        locations=(LocationType.HOME, LocationType.GYM),
        equipment=("chair",),
        patterns=("squat",),
        tags=("standing", "bodyweight"),
    ),
    _exercise(
        "bodyweight_good_morning",
        "Bodyweight Good Morning",
        locations=(LocationType.HOME, LocationType.GYM, LocationType.OUTDOOR),
        patterns=("hinge",),
        tags=("standing", "bodyweight"),
    ),
    _exercise(
        "glute_bridge",
        "Glute Bridge",
        locations=(LocationType.HOME, LocationType.GYM),
        patterns=("hinge",),
        tags=("bodyweight", "floor_required"),
    ),
    _exercise(
        "dead_bug",
        "Dead Bug",
        locations=(LocationType.HOME, LocationType.GYM),
        patterns=("core_stability",),
        tags=("bodyweight", "floor_required"),
    ),
    _exercise(
        "standing_calf_raise",
        "Standing Calf Raise",
        locations=(LocationType.HOME, LocationType.GYM, LocationType.OUTDOOR),
        patterns=("calf_raise",),
        tags=("standing", "bodyweight"),
    ),
    _exercise(
        "resistance_band_row",
        "Resistance Band Row",
        locations=(LocationType.HOME, LocationType.GYM, LocationType.OUTDOOR),
        equipment=("resistance_band",),
        patterns=("horizontal_pull",),
        tags=("standing",),
    ),
    _exercise(
        "resistance_band_chest_press",
        "Resistance Band Chest Press",
        locations=(LocationType.HOME, LocationType.GYM, LocationType.OUTDOOR),
        equipment=("resistance_band",),
        patterns=("horizontal_push",),
        tags=("standing",),
    ),
    _exercise(
        "resistance_band_pull_apart",
        "Resistance Band Pull-Apart",
        locations=(LocationType.HOME, LocationType.GYM, LocationType.OUTDOOR),
        equipment=("resistance_band",),
        patterns=("horizontal_pull",),
        tags=("standing",),
    ),
    _exercise(
        "dumbbell_goblet_squat",
        "Dumbbell Goblet Squat",
        difficulty=ExerciseDifficulty.INTERMEDIATE,
        locations=(LocationType.HOME, LocationType.GYM),
        equipment=("dumbbell",),
        patterns=("squat",),
        tags=("standing",),
    ),
    _exercise(
        "dumbbell_row",
        "Dumbbell Row",
        difficulty=ExerciseDifficulty.INTERMEDIATE,
        locations=(LocationType.HOME, LocationType.GYM),
        equipment=("dumbbell",),
        patterns=("horizontal_pull",),
        tags=("standing",),
    ),
    _exercise(
        "dumbbell_floor_press",
        "Dumbbell Floor Press",
        difficulty=ExerciseDifficulty.INTERMEDIATE,
        locations=(LocationType.HOME, LocationType.GYM),
        equipment=("dumbbell", "yoga_mat"),
        patterns=("horizontal_push",),
        tags=("floor_required",),
    ),
    _exercise(
        "dumbbell_overhead_press",
        "Dumbbell Overhead Press",
        difficulty=ExerciseDifficulty.INTERMEDIATE,
        locations=(LocationType.HOME, LocationType.GYM),
        equipment=("dumbbell",),
        patterns=("vertical_push",),
        tags=("standing", "overhead"),
    ),
    _exercise(
        "brisk_walk",
        "Brisk Walk",
        locations=(LocationType.OUTDOOR,),
        patterns=("locomotion",),
        tags=("outdoor", "low_impact"),
        duration=300,
    ),
    _exercise(
        "march_in_place",
        "March in Place",
        locations=(LocationType.HOME, LocationType.GYM),
        patterns=("locomotion",),
        tags=("standing", "low_impact"),
        duration=120,
    ),
    _exercise(
        "low_impact_step_touch",
        "Low-Impact Step Touch",
        locations=(LocationType.HOME, LocationType.GYM),
        patterns=("lateral_locomotion",),
        tags=("standing", "low_impact"),
        duration=120,
    ),
    _exercise(
        "standing_mobility",
        "Standing Mobility Flow",
        locations=(LocationType.HOME, LocationType.GYM, LocationType.OUTDOOR),
        patterns=("mobility",),
        tags=("standing",),
        duration=180,
    ),
    _exercise(
        "cat_cow_mobility",
        "Cat-Cow Mobility",
        locations=(LocationType.HOME, LocationType.GYM),
        equipment=("yoga_mat",),
        patterns=("mobility",),
        tags=("floor_required",),
        duration=90,
    ),
    _exercise(
        "hip_flexor_stretch",
        "Hip Flexor Stretch",
        locations=(LocationType.HOME, LocationType.GYM),
        equipment=("yoga_mat",),
        patterns=("stretch",),
        tags=("floor_required",),
        duration=60,
    ),
    _exercise(
        "seated_hamstring_stretch",
        "Seated Hamstring Stretch",
        locations=(LocationType.HOME, LocationType.GYM, LocationType.OUTDOOR),
        patterns=("stretch",),
        tags=("seated",),
        duration=60,
    ),
    _exercise(
        "jumping_jack",
        "Jumping Jack",
        difficulty=ExerciseDifficulty.INTERMEDIATE,
        locations=(LocationType.HOME, LocationType.GYM, LocationType.OUTDOOR),
        patterns=("locomotion",),
        tags=("jumping", "high_impact"),
        duration=60,
    ),
    _exercise(
        "easy_jog",
        "Easy Jog",
        difficulty=ExerciseDifficulty.INTERMEDIATE,
        locations=(LocationType.OUTDOOR,),
        patterns=("locomotion",),
        tags=("outdoor", "running", "high_impact"),
        duration=300,
    ),
    _exercise(
        "burpee",
        "Burpee",
        difficulty=ExerciseDifficulty.INTERMEDIATE,
        locations=(LocationType.HOME, LocationType.GYM, LocationType.OUTDOOR),
        patterns=("squat", "horizontal_push", "locomotion"),
        tags=("floor_required", "jumping", "high_impact"),
        duration=45,
        status=ExerciseStatus.DISABLED,
    ),
)

# Concise alias used by application composition code.
CATALOG_SEED = EXERCISE_CATALOG_SEED


def get_catalog_seed() -> tuple[Exercise, ...]:
    """Return the immutable exercise tuple used to initialize an adapter."""

    return EXERCISE_CATALOG_SEED
