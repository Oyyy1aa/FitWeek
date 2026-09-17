"""Application orchestration for deterministic, revision-preserving local changes."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid5

from app.application.errors import (
    BusinessRuleViolation,
    ConflictError,
    IdempotencyConflict,
    LocalReplanningFailed,
    PlanVersionConflict,
    ResourceNotFound,
    RevisionConfirmationConflict,
    RevisionNotFound,
)
from app.domain.checkins.repositories import CheckInRepository
from app.domain.common import (
    DomainConflictError,
    DomainValidationError,
    InvalidDomainStateTransition,
    RepositoryError,
    RepositoryUniqueError,
    utc_now,
)
from app.domain.exercises.models import Exercise
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.plans.models import WeeklyPlan
from app.domain.plans.repositories import PlanRepository
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    UserConstraint,
)
from app.domain.profiles.repositories import ProfileRepository
from app.domain.replanning.models import (
    LocalReplanCommand,
    LocalReplanningError,
    PlanChangeMetadata,
    PlanChangeType,
    ReplanningFailureReason,
)
from app.domain.replanning.policies import (
    MAX_REPAIR_ATTEMPTS,
    REPLANNING_POLICY_VERSION,
)
from app.domain.users.models import UserAccount
from app.replanning.impact_analyzer import ChangeImpactAnalyzer
from app.replanning.local_replanner import LocalReplanner
from app.replanning.revision_builder import PlanRevisionBuilder
from app.safety.engine import SafetyEngine


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalReplanningResult:
    plan: WeeklyPlan
    created: bool
    repair_attempts: int


class LocalReplanningService:
    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        exercises: ExerciseRepository,
        plans: PlanRepository,
        check_ins: CheckInRepository,
        safety_engine: SafetyEngine,
        impact_analyzer: ChangeImpactAnalyzer | None = None,
        replanner: LocalReplanner | None = None,
        revision_builder: PlanRevisionBuilder | None = None,
    ) -> None:
        self._profiles = profiles
        self._exercises = exercises
        self._plans = plans
        self._check_ins = check_ins
        self._safety = safety_engine
        self._impact = impact_analyzer or ChangeImpactAnalyzer()
        self._replanner = replanner or LocalReplanner()
        self._revision_builder = revision_builder or PlanRevisionBuilder()

    async def replan(
        self,
        user: UserAccount,
        plan_id: UUID,
        command: LocalReplanCommand,
    ) -> LocalReplanningResult:
        requested = await self._plans.get_for_user(plan_id, user.id)
        if requested is None:
            raise ResourceNotFound("Weekly plan was not found.")
        existing = await self._plans.find_by_replan_request(
            user.id, command.client_request_id
        )
        if existing is not None:
            parent = await self._plans.get_revision_for_user(
                existing.series_id,
                user.id,
                existing.parent_revision or 0,
            )
            fingerprint = (
                "" if parent is None else self._fingerprint(user.id, parent, command)
            )
            if (
                isinstance(existing.change_metadata, PlanChangeMetadata)
                and existing.change_metadata.change_fingerprint == fingerprint
            ):
                return LocalReplanningResult(
                    plan=existing, created=False, repair_attempts=0
                )
            raise IdempotencyConflict(
                "client_request_id was already used with a different payload."
            )
        source = await self._plans.get_current_confirmed(requested.series_id, user.id)
        if source is None:
            raise BusinessRuleViolation(
                "Only a confirmed plan can be locally replanned.",
                code="PLAN_NOT_CONFIRMED",
            )
        fingerprint = self._fingerprint(user.id, source, command)
        if command.expected_plan_version != source.version:
            raise PlanVersionConflict("The expected plan version is stale.")
        self._validate_command_week(source, command)

        profile = await self._profiles.get_by_user_id(user.id)
        if profile is None:
            raise ResourceNotFound("Fitness profile was not found.")
        constraints = tuple(await self._profiles.list_constraints(profile.id))
        effective_constraints = self._effective_constraints(
            profile.id, constraints, command
        )
        catalog = await self._load_catalog(source)
        all_check_ins = await self._check_ins.list_by_series(source.series_id)
        source_session_ids = {item.id for item in source.sessions}
        check_ins = [
            item for item in all_check_ins if item.session_id in source_session_ids
        ]
        impact = self._impact.analyze(
            plan=source,
            check_ins=check_ins,
            change=command,
            exercise_catalog=catalog,
        )
        immutable_conflicts = tuple(
            ReplanningFailureReason(
                code=item.code,
                message=item.message,
                session_id=item.session_id,
            )
            for item in impact.reasons
            if item.code == "IMMUTABLE_SESSION_CONFLICT"
        )
        if immutable_conflicts:
            raise LocalReplanningFailed(immutable_conflicts)
        if not impact.affected_session_ids:
            raise BusinessRuleViolation(
                "The change does not affect any mutable future session.",
                code="NO_AFFECTED_SESSIONS",
            )

        repair_attempts = 0
        failure_reasons: tuple[ReplanningFailureReason, ...] = ()
        while repair_attempts <= MAX_REPAIR_ATTEMPTS:
            try:
                sessions = self._replanner.rebuild(
                    source=source,
                    profile=profile,
                    constraints=effective_constraints,
                    catalog=catalog,
                    change=command,
                    impact=impact,
                    fingerprint=fingerprint,
                    variant=repair_attempts,
                )
            except LocalReplanningError as exc:
                failure_reasons = exc.reasons
                break
            candidate = self._revision_builder.build(
                source=source,
                sessions=sessions,
                constraint_snapshot=self._constraint_snapshot(effective_constraints),
                change_type=command.change_type,
                impact=impact,
                client_request_id=command.client_request_id,
                fingerprint=fingerprint,
                policy_version=REPLANNING_POLICY_VERSION,
                created_at=utc_now(),
            )
            validation = self._safety.validate_plan(
                profile=profile,
                constraints=effective_constraints,
                plan=candidate,
                exercise_catalog=catalog,
            )
            if validation.passed:
                try:
                    saved = await self._plans.save(candidate)
                except RepositoryUniqueError as exc:
                    if exc.constraint == "weekly_plan.user_replan_request":
                        concurrent = await self._plans.find_by_replan_request(
                            user.id, command.client_request_id
                        )
                        if (
                            concurrent is not None
                            and isinstance(
                                concurrent.change_metadata, PlanChangeMetadata
                            )
                            and concurrent.change_metadata.change_fingerprint
                            == fingerprint
                        ):
                            return LocalReplanningResult(
                                plan=concurrent,
                                created=False,
                                repair_attempts=repair_attempts,
                            )
                    raise ConflictError(
                        "A plan revision conflicts with stored data."
                    ) from exc
                except RepositoryError as exc:
                    raise ConflictError(
                        "A plan revision conflicts with stored data."
                    ) from exc
                return LocalReplanningResult(
                    plan=saved,
                    created=True,
                    repair_attempts=repair_attempts,
                )
            failure_reasons = tuple(
                ReplanningFailureReason(
                    code=item.code,
                    message=item.message,
                    session_id=item.session_id,
                )
                for item in validation.violations
            )
            repair_attempts += 1
        raise LocalReplanningFailed(failure_reasons)

    async def list_revisions(
        self, user: UserAccount, plan_id: UUID
    ) -> list[WeeklyPlan]:
        requested = await self._plans.get_for_user(plan_id, user.id)
        if requested is None:
            raise ResourceNotFound("Weekly plan was not found.")
        return await self._plans.list_revisions_for_user(requested.series_id, user.id)

    async def get_revision(
        self, user: UserAccount, plan_id: UUID, revision: int
    ) -> WeeklyPlan:
        requested = await self._plans.get_for_user(plan_id, user.id)
        if requested is None:
            raise ResourceNotFound("Weekly plan was not found.")
        result = await self._plans.get_revision_for_user(
            requested.series_id, user.id, revision
        )
        if result is None:
            raise RevisionNotFound("Plan revision was not found.")
        return result

    async def confirm_revision(
        self,
        user: UserAccount,
        plan_id: UUID,
        revision: int,
        *,
        expected_version: int,
    ) -> WeeklyPlan:
        target = await self.get_revision(user, plan_id, revision)
        try:
            confirmed_at = max(utc_now(), target.created_at)
            confirmed = target.confirm(
                expected_version=expected_version,
                confirmed_at=confirmed_at,
            )
        except (DomainConflictError, InvalidDomainStateTransition) as exc:
            raise RevisionConfirmationConflict(
                "The revision cannot be confirmed with the supplied version."
            ) from exc
        except DomainValidationError as exc:
            raise BusinessRuleViolation(str(exc), code=exc.code) from exc
        try:
            return await self._plans.save(confirmed)
        except RepositoryError as exc:
            raise RevisionConfirmationConflict(
                "The revision was concurrently modified."
            ) from exc

    async def is_current_revision(self, user: UserAccount, plan: WeeklyPlan) -> bool:
        current = await self._plans.get_current_confirmed(plan.series_id, user.id)
        return current is not None and current.id == plan.id

    async def _load_catalog(self, source: WeeklyPlan) -> dict[str, Exercise]:
        active = await self._exercises.list_active()
        catalog = {item.id: item for item in active}
        for exercise_id in {
            item.exercise_id
            for session in source.sessions
            for item in session.exercises
        }:
            if exercise_id not in catalog:
                exercise = await self._exercises.get(exercise_id)
                if exercise is not None:
                    catalog[exercise_id] = exercise
        return catalog

    @staticmethod
    def _validate_command_week(plan: WeeklyPlan, command: LocalReplanCommand) -> None:
        start = datetime.combine(plan.week_start, time.min, tzinfo=UTC)
        end = start + timedelta(days=7)
        if not start <= command.effective_from < end:
            raise BusinessRuleViolation(
                "effective_from must fall within the plan week.",
                code="LOCAL_REPLANNING_FAILED",
            )
        if any(
            slot.start_utc < start or slot.end_utc > end
            for slot in command.replacement_availability_slots
        ):
            raise BusinessRuleViolation(
                "Replacement slots must be inside the plan week.",
                code="LOCAL_REPLANNING_FAILED",
            )

    @staticmethod
    def _effective_constraints(
        profile_id: UUID,
        constraints: tuple[UserConstraint, ...],
        command: LocalReplanCommand,
    ) -> tuple[UserConstraint, ...]:
        if command.change_type is PlanChangeType.EQUIPMENT_CHANGED:
            result = [
                item
                for item in constraints
                if item.constraint_type is not ConstraintType.AVAILABLE_EQUIPMENT
            ]
            values = command.available_equipment or ()
            constraint_type = ConstraintType.AVAILABLE_EQUIPMENT
        elif command.change_type is PlanChangeType.SESSION_DURATION_CHANGED:
            result = list(constraints)
            values = (str(command.max_session_minutes),)
            constraint_type = ConstraintType.MAX_SESSION_MINUTES
        elif command.change_type is PlanChangeType.EXCLUDED_FEATURE_CHANGED:
            result = list(constraints)
            values = command.excluded_features or ()
            constraint_type = ConstraintType.EXCLUDED_FEATURE
        else:
            return constraints
        for value in values:
            result.append(
                UserConstraint(
                    id=uuid5(
                        profile_id,
                        f"replan:{command.client_request_id}:{constraint_type}:{value}",
                    ),
                    profile_id=profile_id,
                    constraint_type=constraint_type,
                    constraint_value=value,
                    priority=100,
                    is_hard=True,
                    source=ConstraintSource.SYSTEM,
                    valid_until=None,
                    created_at=command.effective_from,
                    version=1,
                )
            )
        return tuple(result)

    @staticmethod
    def _constraint_snapshot(
        constraints: tuple[UserConstraint, ...],
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "id": str(item.id),
                "constraint_type": item.constraint_type.value,
                "constraint_value": item.constraint_value,
                "priority": item.priority,
                "is_hard": item.is_hard,
                "source": item.source.value,
                "valid_until": (
                    item.valid_until.isoformat()
                    if item.valid_until is not None
                    else None
                ),
                "version": item.version,
            }
            for item in constraints
        )

    @staticmethod
    def _fingerprint(
        user_id: UUID, source: WeeklyPlan, command: LocalReplanCommand
    ) -> str:
        payload = {
            "user_id": str(user_id),
            "plan_id": str(source.series_id),
            "source_revision": source.revision,
            "expected_plan_version": command.expected_plan_version,
            "change_type": command.change_type.value,
            "effective_from": command.effective_from.isoformat(),
            "replacement_slots": [
                {
                    "start": item.start_utc.isoformat(),
                    "end": item.end_utc.isoformat(),
                    "location": item.location_type.value,
                }
                for item in sorted(
                    command.replacement_availability_slots,
                    key=lambda value: (
                        value.start_utc,
                        value.end_utc,
                        value.location_type.value,
                    ),
                )
            ],
            "equipment": command.available_equipment,
            "max_duration": command.max_session_minutes,
            "excluded_features": command.excluded_features,
            "policy_version": REPLANNING_POLICY_VERSION,
        }
        canonical = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()
