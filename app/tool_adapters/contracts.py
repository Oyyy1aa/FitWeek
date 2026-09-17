"""Small typed contracts for the seven explicitly registered tools."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.calendar_operations.models import CalendarEventPayload
from app.domain.common import LocationType
from app.domain.exercises.models import Exercise, ExerciseDifficulty, ExerciseStatus
from app.domain.memory.enums import MemoryCandidateStatus
from app.domain.plans.models import WeeklyPlan
from app.domain.recovery.models import RecoveryActionCandidate
from app.domain.session_design.models import SessionDurationBreakdown
from app.domain.sessions.models import SessionExercise
from app.memory.candidate_service import CreateCandidateCommand


class EmptyRequest(BaseModel):
    pass


class ReferenceResponse(BaseModel):
    reference: str = Field(min_length=1)


class MemoryCandidateCreateRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    user_id: UUID
    command: CreateCandidateCommand


class MemoryCandidateCreateResponse(BaseModel):
    candidate_id: UUID
    status: MemoryCandidateStatus
    version: int = Field(ge=1)
    idempotent_reuse: bool
    result_fingerprint: str = Field(min_length=64, max_length=64)


class IcsExportRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    user_id: str = Field(min_length=1)
    plan: WeeklyPlan
    as_of: datetime


class IcsExportResponse(BaseModel):
    content: bytes
    event_count: int = Field(ge=0)


class CalendarFreeBusyRequest(BaseModel):
    start: datetime
    end: datetime
    timezone: str = Field(min_length=1)


class BusyIntervalResponse(BaseModel):
    start: datetime
    end: datetime


class CalendarFreeBusyResponse(BaseModel):
    busy: tuple[BusyIntervalResponse, ...]


class CalendarCommitRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    calendar_id: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    operation: str = Field(pattern="^(CREATE|UPDATE|DELETE)$")
    external_event_id: str | None = None
    payload: CalendarEventPayload | None = None


class CalendarCommitResponse(BaseModel):
    external_event_id: str | None = None
    response_reference: str = Field(min_length=1)


class SessionDurationRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    exercises: tuple[SessionExercise, ...]
    target_duration_minutes: int = Field(ge=15, le=60)


class SessionDurationResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    exercises: tuple[SessionExercise, ...]
    duration: SessionDurationBreakdown
    policy_version: str


class ExerciseToolView(BaseModel):
    exercise_id: str
    name: str
    difficulty: str
    supported_locations: tuple[str, ...]
    required_equipment: tuple[str, ...]
    movement_patterns: tuple[str, ...]
    feature_tags: tuple[str, ...]
    default_duration_seconds: int = Field(gt=0)
    status: str
    version: int = Field(ge=1)

    def to_domain(self) -> Exercise:
        return Exercise(
            id=self.exercise_id,
            name=self.name,
            difficulty_level=ExerciseDifficulty(self.difficulty),
            location_types=frozenset(LocationType(x) for x in self.supported_locations),
            required_equipment=frozenset(self.required_equipment),
            movement_patterns=frozenset(self.movement_patterns),
            feature_tags=frozenset(self.feature_tags),
            default_duration_seconds=self.default_duration_seconds,
            status=ExerciseStatus(self.status),
            version=self.version,
        )


class ExerciseCatalogSearchRequest(BaseModel):
    user_id: UUID
    include_disabled: bool = False
    allowed_locations: tuple[str, ...] = ()
    available_equipment: tuple[str, ...] = ()
    excluded_features: tuple[str, ...] = ()
    allowed_difficulties: tuple[str, ...] = ()
    required_movement_patterns: tuple[str, ...] = ()


class ExerciseCatalogSearchResponse(BaseModel):
    exercises: tuple[ExerciseToolView, ...]
    catalog_version: str
    result_fingerprint: str


class RecoverySpacingRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    plan: WeeklyPlan
    selected: tuple[RecoveryActionCandidate, ...]


class RecoverySpacingResponse(BaseModel):
    passed: bool
    violation_codes: tuple[str, ...]
    affected_session_ids: tuple[UUID, ...]
    policy_version: str = "recovery-spacing-v1"
