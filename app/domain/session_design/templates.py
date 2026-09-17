"""Immutable, code-owned session template values."""

from dataclasses import dataclass

from app.domain.common import DomainValidationError, LocationType
from app.domain.profiles.models import ExperienceLevel, FitnessGoal
from app.domain.session_design.enums import SessionExerciseRole, SessionTemplateId
from app.domain.sessions.models import SessionType


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionTemplateSlot:
    slot_id: str
    role: SessionExerciseRole
    required_patterns: frozenset[str]
    preferred_tags: frozenset[str] = frozenset()
    excluded_feature_tags: frozenset[str] = frozenset()
    min_count: int = 1
    max_count: int = 1
    min_duration_seconds: int | None = None
    max_duration_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.slot_id.strip():
            raise DomainValidationError("template slot_id must not be blank.")
        if not self.required_patterns:
            raise DomainValidationError(
                "template slot must require a movement pattern."
            )
        if not 1 <= self.min_count <= self.max_count:
            raise DomainValidationError("template Slot cardinality is invalid.")
        if (
            self.min_duration_seconds is not None
            and self.max_duration_seconds is not None
            and self.min_duration_seconds > self.max_duration_seconds
        ):
            raise DomainValidationError("template Slot duration bounds are invalid.")


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionTemplate:
    id: SessionTemplateId
    session_type: SessionType
    supported_goals: tuple[FitnessGoal, ...]
    supported_levels: tuple[ExperienceLevel, ...]
    supported_locations: tuple[LocationType, ...]
    min_duration_minutes: int
    max_duration_minutes: int
    slots: tuple[SessionTemplateSlot, ...]
    version: str = "session-template-v1"

    def __post_init__(self) -> None:
        if not 3 <= len(self.slots) <= 6:
            raise DomainValidationError("session template must contain 3 to 6 slots.")
        ids = tuple(item.slot_id for item in self.slots)
        if len(ids) != len(set(ids)):
            raise DomainValidationError("template slot IDs must be unique.")
        roles = {item.role for item in self.slots}
        if roles != set(SessionExerciseRole):
            raise DomainValidationError(
                "template must include warmup, main, and cooldown."
            )
        if not self.supported_goals or not self.supported_levels:
            raise DomainValidationError("template compatibility lists cannot be empty.")
        if not self.supported_locations:
            raise DomainValidationError("template supported locations cannot be empty.")
        if not 15 <= self.min_duration_minutes <= self.max_duration_minutes <= 60:
            raise DomainValidationError("template duration bounds are invalid.")
