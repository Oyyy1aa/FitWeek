"""Workout session and prescribed exercise models."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.common import (
    DomainValidationError,
    LocationType,
    require_non_blank,
    require_utc_datetime,
    require_version,
)


class SessionType(StrEnum):
    STRENGTH = "STRENGTH"
    CARDIO = "CARDIO"
    MOBILITY = "MOBILITY"
    FLEXIBILITY = "FLEXIBILITY"
    MIXED = "MIXED"


class WorkoutSessionStatus(StrEnum):
    DRAFT = "DRAFT"
    PLANNED = "PLANNED"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionExercise:
    exercise_id: str
    sequence_no: int
    sets: int | None
    repetitions: int | None
    duration_seconds: int | None
    rest_seconds: int

    def __post_init__(self) -> None:
        require_non_blank(self.exercise_id, "exercise_id")
        if (
            isinstance(self.sequence_no, bool)
            or not isinstance(self.sequence_no, int)
            or self.sequence_no < 1
        ):
            raise DomainValidationError("sequence_no must be at least 1.")
        for field_name, value in (
            ("sets", self.sets),
            ("repetitions", self.repetitions),
            ("duration_seconds", self.duration_seconds),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise DomainValidationError(f"{field_name} must be positive when set.")
        if self.repetitions is None and self.duration_seconds is None:
            raise DomainValidationError(
                "at least repetitions or duration_seconds must be provided."
            )
        if (
            isinstance(self.rest_seconds, bool)
            or not isinstance(self.rest_seconds, int)
            or self.rest_seconds < 0
        ):
            raise DomainValidationError("rest_seconds must not be negative.")


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkoutSession:
    """A valid immutable workout session within the Phase 1A domain."""

    id: UUID
    plan_id: UUID
    scheduled_start: datetime
    scheduled_end: datetime
    location_type: LocationType
    session_type: SessionType
    estimated_minutes: int
    target_difficulty: int
    status: WorkoutSessionStatus
    exercises: tuple[SessionExercise, ...]
    version: int
    schedule_source_metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.plan_id, UUID):
            raise DomainValidationError("id and plan_id must be UUID values.")
        require_utc_datetime(self.scheduled_start, "scheduled_start")
        require_utc_datetime(self.scheduled_end, "scheduled_end")
        if self.scheduled_start >= self.scheduled_end:
            raise DomainValidationError(
                "scheduled_start must be earlier than scheduled_end.",
                code="INVALID_SESSION_TIME",
            )
        if not isinstance(self.location_type, LocationType):
            raise DomainValidationError("location_type must be a LocationType.")
        if not isinstance(self.session_type, SessionType):
            raise DomainValidationError("session_type must be a SessionType.")
        if not isinstance(self.status, WorkoutSessionStatus):
            raise DomainValidationError("status must be a WorkoutSessionStatus.")
        if (
            isinstance(self.estimated_minutes, bool)
            or not isinstance(self.estimated_minutes, int)
            or not 15 <= self.estimated_minutes <= 60
        ):
            raise DomainValidationError("estimated_minutes must be between 15 and 60.")
        if (
            isinstance(self.target_difficulty, bool)
            or not isinstance(self.target_difficulty, int)
            or not 1 <= self.target_difficulty <= 10
        ):
            raise DomainValidationError("target_difficulty must be between 1 and 10.")
        if not isinstance(self.exercises, tuple) or any(
            not isinstance(item, SessionExercise) for item in self.exercises
        ):
            raise DomainValidationError(
                "exercises must be a tuple of SessionExercise values."
            )
        if not self.exercises:
            raise DomainValidationError("exercises must not be empty.")
        sequence_numbers = [item.sequence_no for item in self.exercises]
        if len(sequence_numbers) != len(set(sequence_numbers)):
            raise DomainValidationError(
                "exercise sequence_no values must be unique.",
                code="DUPLICATE_SEQUENCE",
            )
        require_version(self.version)
        if any(
            not key.strip() or not value.strip()
            for key, value in self.schedule_source_metadata
        ):
            raise DomainValidationError(
                "schedule_source_metadata values must be non-blank."
            )
