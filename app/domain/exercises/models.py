"""Exercise catalog entities."""

import re
from dataclasses import dataclass
from enum import StrEnum

from app.domain.common import (
    DomainValidationError,
    LocationType,
    require_non_blank,
    require_version,
)

_EXERCISE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


class ExerciseDifficulty(StrEnum):
    BEGINNER = "BEGINNER"
    INTERMEDIATE = "INTERMEDIATE"


class ExerciseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True, kw_only=True)
class Exercise:
    id: str
    name: str
    difficulty_level: ExerciseDifficulty
    location_types: frozenset[LocationType]
    required_equipment: frozenset[str]
    movement_patterns: frozenset[str]
    feature_tags: frozenset[str]
    default_duration_seconds: int
    status: ExerciseStatus
    version: int

    def __post_init__(self) -> None:
        require_non_blank(self.id, "id")
        if _EXERCISE_ID_PATTERN.fullmatch(self.id) is None:
            raise DomainValidationError(
                "exercise id must be a stable lower_snake_case identifier."
            )
        require_non_blank(self.name, "name")
        if not isinstance(self.difficulty_level, ExerciseDifficulty):
            raise DomainValidationError(
                "difficulty_level must be an ExerciseDifficulty."
            )
        if not isinstance(self.status, ExerciseStatus):
            raise DomainValidationError("status must be an ExerciseStatus.")
        if not isinstance(self.location_types, frozenset) or any(
            not isinstance(location, LocationType) for location in self.location_types
        ):
            raise DomainValidationError(
                "location_types must be a frozenset of LocationType values."
            )
        if not self.location_types:
            raise DomainValidationError("location_types must not be empty.")
        self._validate_string_set(self.required_equipment, "required_equipment")
        self._validate_string_set(self.movement_patterns, "movement_patterns")
        self._validate_string_set(self.feature_tags, "feature_tags")
        if not self.movement_patterns:
            raise DomainValidationError("movement_patterns must not be empty.")
        if (
            isinstance(self.default_duration_seconds, bool)
            or not isinstance(self.default_duration_seconds, int)
            or self.default_duration_seconds <= 0
        ):
            raise DomainValidationError("default_duration_seconds must be positive.")
        require_version(self.version)

    @staticmethod
    def _validate_string_set(values: frozenset[str], field_name: str) -> None:
        if not isinstance(values, frozenset):
            raise DomainValidationError(f"{field_name} must be a frozenset.")
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise DomainValidationError(f"{field_name} cannot contain blank values.")
