"""Explicit HTTP request and response DTOs for the Phase 1A API."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.application.plan_generation import PlanGenerationResult
from app.application.plans import PlanCreationResult
from app.application.profiles import ProfileView
from app.domain.common import LocationType
from app.domain.exercises.models import (
    Exercise,
    ExerciseDifficulty,
    ExerciseStatus,
)
from app.domain.planning.models import AvailabilitySlot, GenerateWeeklyPlanCommand
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
    UserConstraint,
)
from app.domain.recovery_application.models import RecoveryPlanApplicationMetadata
from app.domain.replanning.models import PlanChangeMetadata
from app.domain.schedule_application.models import ScheduleApplicationMetadata
from app.domain.session_design_application.models import (
    SessionDesignApplicationMetadata,
)
from app.domain.sessions.models import (
    SessionExercise,
    SessionType,
    WorkoutSession,
    WorkoutSessionStatus,
)
from app.safety.models import SafetyValidationResult, SafetyViolation


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileUpsertRequest(RequestModel):
    experience_level: ExperienceLevel
    weekly_frequency: int = Field(ge=2, le=5)
    max_session_minutes: int = Field(ge=15, le=60)
    primary_goal: FitnessGoal
    scope_confirmed: bool
    expected_version: int | None = Field(default=None, ge=0)


class ConstraintCreateRequest(RequestModel):
    constraint_type: ConstraintType
    constraint_value: str = Field(min_length=1, max_length=200)
    priority: int = Field(default=100, ge=0)
    is_hard: bool = True
    source: ConstraintSource = ConstraintSource.USER_EXPLICIT
    valid_until: datetime | None = None

    @field_validator("valid_until")
    @classmethod
    def require_utc_expiration(cls, value: datetime | None) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("valid_until must be timezone-aware UTC")
        return value


class ConstraintResponse(BaseModel):
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

    @classmethod
    def from_domain(cls, value: UserConstraint) -> Self:
        return cls(
            id=value.id,
            profile_id=value.profile_id,
            constraint_type=value.constraint_type,
            constraint_value=value.constraint_value,
            priority=value.priority,
            is_hard=value.is_hard,
            source=value.source,
            valid_until=value.valid_until,
            created_at=value.created_at,
            version=value.version,
        )


class ProfileResponse(BaseModel):
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
    constraints: list[ConstraintResponse]

    @classmethod
    def from_view(cls, value: ProfileView) -> Self:
        profile = value.profile
        return cls(
            id=profile.id,
            user_id=profile.user_id,
            experience_level=profile.experience_level,
            weekly_frequency=profile.weekly_frequency,
            max_session_minutes=profile.max_session_minutes,
            primary_goal=profile.primary_goal,
            scope_confirmed=profile.scope_confirmed,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
            version=profile.version,
            constraints=[
                ConstraintResponse.from_domain(item) for item in value.constraints
            ],
        )


class ExerciseResponse(BaseModel):
    id: str
    name: str
    difficulty_level: ExerciseDifficulty
    location_types: list[LocationType]
    required_equipment: list[str]
    movement_patterns: list[str]
    feature_tags: list[str]
    default_duration_seconds: int
    status: ExerciseStatus
    version: int

    @classmethod
    def from_domain(cls, value: Exercise) -> Self:
        return cls(
            id=value.id,
            name=value.name,
            difficulty_level=value.difficulty_level,
            location_types=sorted(value.location_types, key=lambda item: item.value),
            required_equipment=sorted(value.required_equipment),
            movement_patterns=sorted(value.movement_patterns),
            feature_tags=sorted(value.feature_tags),
            default_duration_seconds=value.default_duration_seconds,
            status=value.status,
            version=value.version,
        )


class SessionExerciseRequest(RequestModel):
    exercise_id: str = Field(min_length=1)
    sequence_no: int = Field(ge=1)
    sets: int | None = Field(default=None, ge=1)
    repetitions: int | None = Field(default=None, ge=1)
    duration_seconds: int | None = Field(default=None, ge=1)
    rest_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_repetitions_or_duration(self) -> Self:
        if self.repetitions is None and self.duration_seconds is None:
            raise ValueError("repetitions or duration_seconds is required")
        return self


class WorkoutSessionRequest(RequestModel):
    scheduled_start: datetime
    scheduled_end: datetime
    location_type: LocationType
    session_type: SessionType
    estimated_minutes: int = Field(gt=0)
    target_difficulty: int = Field(ge=1, le=10)
    exercises: list[SessionExerciseRequest]


class PlanCreateRequest(RequestModel):
    week_start: date
    revision: int = Field(default=1, ge=1)
    sessions: list[WorkoutSessionRequest]


class AvailabilitySlotRequest(RequestModel):
    start: datetime
    end: datetime
    location_type: LocationType

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if (
            self.start.tzinfo is None
            or self.start.utcoffset() is None
            or self.end.tzinfo is None
            or self.end.utcoffset() is None
        ):
            raise ValueError("availability slot timestamps must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("availability slot start must be earlier than end")
        if self.end.astimezone(UTC) - self.start.astimezone(UTC) < timedelta(
            minutes=15
        ):
            raise ValueError("availability slots must be at least 15 minutes")
        return self

    def to_domain(self) -> AvailabilitySlot:
        return AvailabilitySlot(
            start=self.start,
            end=self.end,
            location_type=self.location_type,
        )


class GenerateWeeklyPlanRequest(RequestModel):
    client_request_id: str | None = Field(default=None, min_length=1, max_length=120)
    week_start: date
    availability_slots: list[AvailabilitySlotRequest] = Field(
        min_length=1,
        max_length=21,
    )
    preferred_locations: list[LocationType] = Field(default_factory=list)
    preferred_session_types: list[SessionType] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_week_and_slots(self) -> Self:
        if self.week_start.weekday() != 0:
            raise ValueError("week_start must be a Monday")
        week_start = datetime.combine(self.week_start, datetime.min.time(), tzinfo=UTC)
        week_end = week_start + timedelta(days=7)
        ordered = sorted(
            self.availability_slots,
            key=lambda slot: (
                slot.start.astimezone(UTC),
                slot.end.astimezone(UTC),
                slot.location_type.value,
            ),
        )
        for slot in ordered:
            if (
                slot.start.astimezone(UTC) < week_start
                or slot.end.astimezone(UTC) > week_end
            ):
                raise ValueError("availability slots must be inside the target week")
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.start.astimezone(UTC) < previous.end.astimezone(UTC):
                raise ValueError("availability slots must not overlap")
        return self

    def to_command(self) -> GenerateWeeklyPlanCommand:
        return GenerateWeeklyPlanCommand(
            week_start=self.week_start,
            availability_slots=tuple(
                item.to_domain() for item in self.availability_slots
            ),
            preferred_locations=tuple(self.preferred_locations),
            preferred_session_types=tuple(self.preferred_session_types),
            client_request_id=self.client_request_id,
        )


class PlanConfirmRequest(RequestModel):
    expected_version: int = Field(ge=1)


class SessionExerciseResponse(BaseModel):
    exercise_id: str
    sequence_no: int
    sets: int | None
    repetitions: int | None
    duration_seconds: int | None
    rest_seconds: int

    @classmethod
    def from_domain(cls, value: SessionExercise) -> Self:
        return cls(
            exercise_id=value.exercise_id,
            sequence_no=value.sequence_no,
            sets=value.sets,
            repetitions=value.repetitions,
            duration_seconds=value.duration_seconds,
            rest_seconds=value.rest_seconds,
        )


class WorkoutSessionResponse(BaseModel):
    id: UUID
    plan_id: UUID
    scheduled_start: datetime
    scheduled_end: datetime
    location_type: LocationType
    session_type: SessionType
    estimated_minutes: int
    target_difficulty: int
    status: WorkoutSessionStatus
    exercises: list[SessionExerciseResponse]
    version: int

    @classmethod
    def from_domain(cls, value: WorkoutSession) -> Self:
        return cls(
            id=value.id,
            plan_id=value.plan_id,
            scheduled_start=value.scheduled_start,
            scheduled_end=value.scheduled_end,
            location_type=value.location_type,
            session_type=value.session_type,
            estimated_minutes=value.estimated_minutes,
            target_difficulty=value.target_difficulty,
            status=value.status,
            exercises=[
                SessionExerciseResponse.from_domain(item) for item in value.exercises
            ],
            version=value.version,
        )


class WeeklyPlanResponse(BaseModel):
    id: UUID
    user_id: UUID
    week_start: date
    status: WeeklyPlanStatus
    revision: int
    goal_snapshot: dict[str, Any]
    constraint_snapshot: tuple[dict[str, Any], ...]
    estimated_total_minutes: int
    sessions: list[WorkoutSessionResponse]
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    version: int
    generation_metadata: dict[str, str] | None
    root_plan_id: UUID | None
    parent_revision: int | None
    revision_reason: str | None
    change_metadata: dict[str, Any] | None

    @classmethod
    def from_domain(cls, value: WeeklyPlan) -> Self:
        return cls(
            id=value.id,
            user_id=value.user_id,
            week_start=value.week_start,
            status=value.status,
            revision=value.revision,
            goal_snapshot=value.goal_snapshot,
            constraint_snapshot=value.constraint_snapshot,
            estimated_total_minutes=value.estimated_total_minutes,
            sessions=[
                WorkoutSessionResponse.from_domain(item) for item in value.sessions
            ],
            created_at=value.created_at,
            updated_at=value.updated_at,
            confirmed_at=value.confirmed_at,
            version=value.version,
            generation_metadata=value.generation_metadata,
            root_plan_id=value.root_plan_id,
            parent_revision=value.parent_revision,
            revision_reason=value.revision_reason,
            change_metadata=cls._change_metadata(value.change_metadata),
        )

    @staticmethod
    def _change_metadata(
        value: (
            PlanChangeMetadata
            | SessionDesignApplicationMetadata
            | ScheduleApplicationMetadata
            | RecoveryPlanApplicationMetadata
            | None
        ),
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        common: dict[str, Any] = {
            "source_revision": value.source_revision,
            "changed_session_ids": [str(item) for item in value.changed_session_ids],
            "preserved_session_ids": [
                str(item) for item in value.preserved_session_ids
            ],
            "immutable_session_ids": [
                str(item) for item in value.immutable_session_ids
            ],
        }
        if isinstance(value, PlanChangeMetadata):
            common.update(
                {
                    "change_type": value.change_type.value,
                    "change_fingerprint": value.change_fingerprint,
                    "replanning_policy_version": (value.replanning_policy_version),
                    "client_request_id": value.client_request_id,
                }
            )
        elif isinstance(value, SessionDesignApplicationMetadata):
            common.update(
                {
                    "source_draft_id": str(value.source_draft_id),
                    "source_draft_version": value.source_draft_version,
                    "source_candidate_set_id": str(value.source_candidate_set_id),
                    "source_context_snapshot_reference_id": str(
                        value.source_context_snapshot_reference_id
                    ),
                    "target_session_id": str(value.target_session_id),
                    "application_fingerprint": value.application_fingerprint,
                    "policy_version": value.policy_version,
                }
            )
        elif isinstance(value, ScheduleApplicationMetadata):
            common.update(
                {
                    "source_schedule_draft_id": str(value.source_schedule_draft_id),
                    "source_schedule_draft_version": (
                        value.source_schedule_draft_version
                    ),
                    "source_context_snapshot_reference_id": str(
                        value.source_context_snapshot_reference_id
                    ),
                    "source_busy_snapshot_id": str(value.source_busy_snapshot_id),
                    "source_candidate_set_id": str(value.source_candidate_set_id),
                    "created_revision": value.created_revision,
                    "calendar_verification_status": (
                        value.calendar_verification_status.value
                    ),
                    "application_fingerprint": value.application_fingerprint,
                    "policy_version": value.policy_version,
                }
            )
        else:
            common.update(
                {
                    "source_recovery_draft_id": str(value.source_recovery_draft_id),
                    "source_candidate_set_id": str(value.source_candidate_set_id),
                    "source_behavior_summary_id": str(value.source_behavior_summary_id),
                    "source_change_impact_snapshot_id": str(
                        value.source_change_impact_snapshot_id
                    ),
                    "created_revision": value.created_revision,
                    "removed_session_ids": [
                        str(item) for item in value.removed_session_ids
                    ],
                    "application_fingerprint": value.application_fingerprint,
                    "policy_version": value.policy_version,
                }
            )
        return common


class SafetyViolationResponse(BaseModel):
    code: str
    message: str
    path: str | None
    session_id: UUID | None
    exercise_id: str | None

    @classmethod
    def from_domain(cls, value: SafetyViolation) -> Self:
        return cls(
            code=value.code,
            message=value.message,
            path=value.path,
            session_id=value.session_id,
            exercise_id=value.exercise_id,
        )


class SafetyValidationResponse(BaseModel):
    passed: bool
    violations: list[SafetyViolationResponse]

    @classmethod
    def from_domain(cls, value: SafetyValidationResult) -> Self:
        return cls(
            passed=value.passed,
            violations=[
                SafetyViolationResponse.from_domain(item) for item in value.violations
            ],
        )


class PlanCreationResponse(BaseModel):
    plan: WeeklyPlanResponse
    validation: SafetyValidationResponse

    @classmethod
    def from_result(cls, value: PlanCreationResult) -> Self:
        return cls(
            plan=WeeklyPlanResponse.from_domain(value.plan),
            validation=SafetyValidationResponse.from_domain(value.validation),
        )


class GenerationMetadataResponse(BaseModel):
    generation_policy_version: str
    catalog_version: str
    input_fingerprint: str
    context_snapshot_reference_id: str
    context_fingerprint: str
    context_contract_version: str
    context_policy_version: str
    context_degradation_state: str
    included_memory_count: str
    shadowed_memory_count: str
    preference_fallbacks: str


class PlanGenerationResponse(BaseModel):
    plan: WeeklyPlanResponse
    validation: SafetyValidationResponse
    generation: GenerationMetadataResponse
    repair_attempts: int

    @classmethod
    def from_result(cls, value: PlanGenerationResult) -> Self:
        return cls(
            plan=WeeklyPlanResponse.from_domain(value.plan),
            validation=SafetyValidationResponse.from_domain(value.validation),
            generation=GenerationMetadataResponse(**value.metadata.as_dict()),
            repair_attempts=value.repair_attempts,
        )
