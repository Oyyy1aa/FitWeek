"""Fitness profile and user constraint entities."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.common import (
    DomainValidationError,
    require_non_blank,
    require_utc_datetime,
    require_version,
)


class ExperienceLevel(StrEnum):
    BEGINNER = "BEGINNER"
    INTERMEDIATE = "INTERMEDIATE"


class FitnessGoal(StrEnum):
    BUILD_HABIT = "BUILD_HABIT"
    GENERAL_FITNESS = "GENERAL_FITNESS"
    BASIC_STRENGTH = "BASIC_STRENGTH"
    LOW_IMPACT_CARDIO = "LOW_IMPACT_CARDIO"
    MOBILITY = "MOBILITY"
    MIXED = "MIXED"


class ConstraintType(StrEnum):
    EXCLUDED_FEATURE = "EXCLUDED_FEATURE"
    AVAILABLE_EQUIPMENT = "AVAILABLE_EQUIPMENT"
    ALLOWED_LOCATION = "ALLOWED_LOCATION"
    UNAVAILABLE_TIME = "UNAVAILABLE_TIME"
    MAX_SESSION_MINUTES = "MAX_SESSION_MINUTES"


class ConstraintSource(StrEnum):
    USER_EXPLICIT = "USER_EXPLICIT"
    USER_CONFIRMED_AGENT_DRAFT = "USER_CONFIRMED_AGENT_DRAFT"
    PROFILE = "PROFILE"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True, slots=True, kw_only=True)
class FitnessProfile:
    id: UUID
    user_id: UUID
    experience_level: ExperienceLevel
    weekly_frequency: int
    max_session_minutes: int
    primary_goal: FitnessGoal
    scope_confirmed: bool
    created_at: datetime
    updated_at: datetime
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.user_id, UUID):
            raise DomainValidationError("id and user_id must be UUID values.")
        if not isinstance(self.experience_level, ExperienceLevel):
            raise DomainValidationError("experience_level must be an ExperienceLevel.")
        if not isinstance(self.primary_goal, FitnessGoal):
            raise DomainValidationError("primary_goal must be a FitnessGoal.")
        if (
            isinstance(self.weekly_frequency, bool)
            or not isinstance(self.weekly_frequency, int)
            or not 2 <= self.weekly_frequency <= 5
        ):
            raise DomainValidationError("weekly_frequency must be between 2 and 5.")
        if (
            isinstance(self.max_session_minutes, bool)
            or not isinstance(self.max_session_minutes, int)
            or not 15 <= self.max_session_minutes <= 60
        ):
            raise DomainValidationError(
                "max_session_minutes must be between 15 and 60."
            )
        if not isinstance(self.scope_confirmed, bool):
            raise DomainValidationError("scope_confirmed must be a boolean.")
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise DomainValidationError("updated_at cannot precede created_at.")
        require_version(self.version)


@dataclass(frozen=True, slots=True, kw_only=True)
class UserConstraint:
    id: UUID
    profile_id: UUID
    constraint_type: ConstraintType
    constraint_value: str
    priority: int
    is_hard: bool
    source: ConstraintSource
    valid_until: datetime | None
    created_at: datetime
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.profile_id, UUID):
            raise DomainValidationError("id and profile_id must be UUID values.")
        if not isinstance(self.constraint_type, ConstraintType):
            raise DomainValidationError("constraint_type must be a ConstraintType.")
        if not isinstance(self.source, ConstraintSource):
            raise DomainValidationError("source must be a ConstraintSource.")
        require_non_blank(self.constraint_value, "constraint_value")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise DomainValidationError("priority must be an integer.")
        if self.priority < 0:
            raise DomainValidationError("priority must not be negative.")
        if not isinstance(self.is_hard, bool):
            raise DomainValidationError("is_hard must be a boolean.")
        require_utc_datetime(self.created_at, "created_at")
        if self.valid_until is not None:
            require_utc_datetime(self.valid_until, "valid_until")
        require_version(self.version)
