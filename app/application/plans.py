"""Structured weekly-plan use cases with deterministic safety validation."""

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from app.application.errors import (
    BusinessRuleViolation,
    ConflictError,
    InvalidStateTransition,
    ResourceNotFound,
)
from app.domain.common import (
    DomainConflictError,
    DomainValidationError,
    InvalidDomainStateTransition,
    LocationType,
    RepositoryError,
    utc_now,
)
from app.domain.exercises.models import Exercise
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.domain.profiles.repositories import ProfileRepository
from app.domain.sessions.models import (
    SessionExercise,
    SessionType,
    WorkoutSession,
    WorkoutSessionStatus,
)
from app.domain.users.models import UserAccount
from app.safety.engine import SafetyEngine
from app.safety.models import SafetyValidationResult


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionExerciseCommand:
    exercise_id: str
    sequence_no: int
    sets: int | None
    repetitions: int | None
    duration_seconds: int | None
    rest_seconds: int


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkoutSessionCommand:
    scheduled_start: datetime
    scheduled_end: datetime
    location_type: LocationType
    session_type: SessionType
    estimated_minutes: int
    target_difficulty: int
    exercises: tuple[SessionExerciseCommand, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CreatePlanCommand:
    week_start: date
    revision: int
    sessions: tuple[WorkoutSessionCommand, ...]


@dataclass(frozen=True, slots=True)
class PlanCreationResult:
    plan: WeeklyPlan
    validation: SafetyValidationResult


class PlanService:
    """Build, validate, save, query, and confirm structured plans."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        exercises: ExerciseRepository,
        plans: PlanRepository,
        safety_engine: SafetyEngine,
    ) -> None:
        self._profiles = profiles
        self._exercises = exercises
        self._plans = plans
        self._safety = safety_engine

    async def create_plan(
        self,
        user: UserAccount,
        command: CreatePlanCommand,
    ) -> PlanCreationResult:
        profile = await self._profiles.get_by_user_id(user.id)
        if profile is None:
            raise ResourceNotFound("Create a fitness profile before creating a plan.")
        constraints = await self._profiles.list_constraints(profile.id)

        try:
            candidate = self._build_candidate(user, profile, constraints, command)
        except DomainValidationError as exc:
            raise BusinessRuleViolation(str(exc), code=exc.code) from exc

        catalog = await self._load_catalog(candidate)
        validation = self._safety.validate_plan(
            profile=profile,
            constraints=constraints,
            plan=candidate,
            exercise_catalog=catalog,
        )
        if not validation.passed:
            raise BusinessRuleViolation(
                "The submitted plan violates one or more hard constraints.",
                violations=validation.violations,
                code="PLAN_SAFETY_VALIDATION_FAILED",
            )

        validated = replace(candidate, status=WeeklyPlanStatus.VALIDATED)
        try:
            saved = await self._plans.save(validated)
        except RepositoryError as exc:
            raise ConflictError(
                "Plan identity or version conflicts with stored data."
            ) from exc
        return PlanCreationResult(plan=saved, validation=validation)

    async def get_plan(self, user: UserAccount, plan_id: UUID) -> WeeklyPlan:
        plan = await self._plans.get_for_user(plan_id, user.id)
        if plan is None:
            raise ResourceNotFound("Weekly plan was not found.")
        return plan

    async def list_plans(self, user: UserAccount) -> list[WeeklyPlan]:
        return await self._plans.list_by_user(user.id)

    async def confirm_plan(
        self,
        user: UserAccount,
        plan_id: UUID,
        *,
        expected_version: int,
    ) -> WeeklyPlan:
        current = await self.get_plan(user, plan_id)
        try:
            confirmed_at = max(utc_now(), current.created_at)
            confirmed = current.confirm(
                expected_version=expected_version,
                confirmed_at=confirmed_at,
            )
        except DomainConflictError as exc:
            raise ConflictError("Plan expected_version is stale.") from exc
        except InvalidDomainStateTransition as exc:
            raise InvalidStateTransition(str(exc)) from exc
        except DomainValidationError as exc:
            raise BusinessRuleViolation(str(exc), code=exc.code) from exc
        try:
            return await self._plans.save(confirmed)
        except RepositoryError as exc:
            raise ConflictError("Plan was concurrently modified.") from exc

    def _build_candidate(
        self,
        user: UserAccount,
        profile: FitnessProfile,
        constraints: list[UserConstraint],
        command: CreatePlanCommand,
    ) -> WeeklyPlan:
        plan_id = uuid4()
        sessions = tuple(
            WorkoutSession(
                id=uuid4(),
                plan_id=plan_id,
                scheduled_start=item.scheduled_start,
                scheduled_end=item.scheduled_end,
                location_type=item.location_type,
                session_type=item.session_type,
                estimated_minutes=item.estimated_minutes,
                target_difficulty=item.target_difficulty,
                status=WorkoutSessionStatus.PLANNED,
                exercises=tuple(
                    SessionExercise(
                        exercise_id=exercise.exercise_id,
                        sequence_no=exercise.sequence_no,
                        sets=exercise.sets,
                        repetitions=exercise.repetitions,
                        duration_seconds=exercise.duration_seconds,
                        rest_seconds=exercise.rest_seconds,
                    )
                    for exercise in item.exercises
                ),
                version=1,
            )
            for item in command.sessions
        )
        now = utc_now()
        goal_snapshot: dict[str, Any] = {
            "experience_level": profile.experience_level.value,
            "primary_goal": profile.primary_goal.value,
            "weekly_frequency": profile.weekly_frequency,
            "max_session_minutes": profile.max_session_minutes,
        }
        constraint_snapshot: tuple[dict[str, Any], ...] = tuple(
            {
                "id": str(constraint.id),
                "constraint_type": constraint.constraint_type.value,
                "constraint_value": constraint.constraint_value,
                "priority": constraint.priority,
                "is_hard": constraint.is_hard,
                "source": constraint.source.value,
                "valid_until": (
                    constraint.valid_until.isoformat()
                    if constraint.valid_until is not None
                    else None
                ),
                "version": constraint.version,
            }
            for constraint in constraints
        )
        return WeeklyPlan(
            id=plan_id,
            user_id=user.id,
            week_start=command.week_start,
            status=WeeklyPlanStatus.DRAFT,
            revision=command.revision,
            goal_snapshot=goal_snapshot,
            constraint_snapshot=constraint_snapshot,
            estimated_total_minutes=sum(
                session.estimated_minutes for session in sessions
            ),
            sessions=sessions,
            created_at=now,
            updated_at=now,
            confirmed_at=None,
            version=1,
        )

    async def _load_catalog(self, plan: WeeklyPlan) -> dict[str, Exercise]:
        exercise_ids = {
            exercise.exercise_id
            for session in plan.sessions
            for exercise in session.exercises
        }
        catalog: dict[str, Exercise] = {}
        for exercise_id in sorted(exercise_ids):
            exercise = await self._exercises.get(exercise_id)
            if exercise is not None:
                catalog[exercise_id] = exercise
        return catalog
