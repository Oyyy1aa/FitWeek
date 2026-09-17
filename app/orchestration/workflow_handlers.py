"""Deterministic handlers for the Phase 2A plan-generation workflow."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import cast
from uuid import UUID

from app.application.contexts import ContextApplicationService
from app.application.errors import ApplicationError
from app.application.plan_generation import PlanGenerationService
from app.domain.common import LocationType
from app.domain.context.enums import AgentType
from app.domain.context.models import ContextBuildCommand
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.orchestration.enums import StepOutcome, StepType
from app.domain.orchestration.models import JsonObject, JsonValue
from app.domain.planning.models import AvailabilitySlot, GenerateWeeklyPlanCommand
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.profiles.repositories import ProfileRepository
from app.domain.sessions.models import SessionType
from app.domain.users.models import UserAccount
from app.orchestration.handler import (
    PermanentStepError,
    StepExecutionContext,
    StepExecutionResult,
)
from app.safety.engine import SafetyEngine


def command_from_payload(payload: JsonObject) -> GenerateWeeklyPlanCommand:
    """Rebuild a validated command from the Run's safe canonical input."""

    try:
        raw_slots = cast(list[JsonValue], payload["availability_slots"])
        slots: list[AvailabilitySlot] = []
        for raw_slot in raw_slots:
            slot = cast(dict[str, JsonValue], raw_slot)
            slots.append(
                AvailabilitySlot(
                    start=datetime.fromisoformat(cast(str, slot["start"])),
                    end=datetime.fromisoformat(cast(str, slot["end"])),
                    location_type=LocationType(cast(str, slot["location_type"])),
                )
            )
        return GenerateWeeklyPlanCommand(
            week_start=date.fromisoformat(cast(str, payload["week_start"])),
            availability_slots=tuple(slots),
            preferred_locations=tuple(
                LocationType(cast(str, item))
                for item in cast(list[JsonValue], payload["preferred_locations"])
            ),
            preferred_session_types=tuple(
                SessionType(cast(str, item))
                for item in cast(list[JsonValue], payload["preferred_session_types"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentStepError("Run generation input is invalid.") from exc


class LoadProfileContextHandler:
    step_type = StepType.LOAD_PROFILE_CONTEXT
    version = "phase-2a-load-profile-v1"

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        exercises: ExerciseRepository,
        contexts: ContextApplicationService,
        user: UserAccount,
    ) -> None:
        self._profiles = profiles
        self._exercises = exercises
        self._contexts = contexts
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        profile = await self._profiles.get_by_user_id(context.claim.run.user_id)
        if profile is None:
            raise PermanentStepError("A fitness profile is required for planning.")
        constraints = await self._profiles.list_constraints(profile.id)
        catalog = await self._exercises.list_active()
        canonical = "|".join(
            f"{item.id}:{item.version}" for item in sorted(catalog, key=lambda x: x.id)
        )
        command = command_from_payload(context.claim.run.input_payload)
        task = {
            "request_type": "deterministic_plan_generation",
            "week_start": command.week_start.isoformat(),
            "availability_count": str(len(command.availability_slots)),
        }
        if command.preferred_locations:
            task["preferred_location"] = command.preferred_locations[0].value
        periods = {
            "morning"
            if slot.start.hour < 12
            else "afternoon"
            if slot.start.hour < 18
            else "evening"
            for slot in command.availability_slots
        }
        if len(periods) == 1:
            task["preferred_time_of_day"] = next(iter(periods))
        snapshot = await self._contexts.build_snapshot(
            self._user,
            ContextBuildCommand(
                agent_type=AgentType.PLAN_GENERATION,
                current_task=task,
                catalog_reference=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                run_id=context.claim.run.id,
                step_id=context.claim.step.id,
            ),
            scope_id=f"plan-run-step:{context.claim.step.id}",
        )
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload={
                "profile_id": str(profile.id),
                "profile_version": profile.version,
                "constraint_count": len(constraints),
                "catalog_version": hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest(),
                "snapshot_reference_id": str(snapshot.reference.id),
                "context_fingerprint": snapshot.reference.context_fingerprint,
                "contract_version": snapshot.reference.contract_version,
                "included_memory_count": len(snapshot.reference.memory_versions),
                "degraded_mode": snapshot.reference.degraded_mode.value,
            },
        )


class GenerateDeterministicPlanHandler:
    step_type = StepType.GENERATE_DETERMINISTIC_PLAN
    version = "phase-2a-generate-plan-v1"

    def __init__(
        self,
        *,
        service: PlanGenerationService,
        user: UserAccount,
    ) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        if context.claim.run.user_id != self._user.id:
            raise PermanentStepError("Run user is outside this development identity.")
        try:
            result = await self._service.generate_plan(
                self._user,
                command_from_payload(context.claim.run.input_payload),
                snapshot_reference_id=self._snapshot_reference_id(
                    context.claim.step.input_payload
                ),
                context_scope_id=f"plan-run-step:{context.claim.step.id}",
                run_id=context.claim.run.id,
                step_id=context.claim.step.id,
            )
        except ApplicationError as exc:
            raise PermanentStepError(
                "Deterministic plan generation was rejected."
            ) from exc
        plan = result.plan
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload={
                "plan_id": str(plan.id),
                "root_plan_id": str(plan.series_id),
                "revision": plan.revision,
                "plan_version": plan.version,
                "snapshot_reference_id": str(
                    result.metadata.context_snapshot_reference_id
                ),
                "context_fingerprint": result.metadata.context_fingerprint,
                "contract_version": result.metadata.context_contract_version,
                "included_memory_count": result.metadata.included_memory_count,
                "shadowed_memory_count": result.metadata.shadowed_memory_count,
                "degraded_mode": context.claim.step.input_payload.get(
                    "degraded_mode", "NONE"
                ),
            },
            result_reference=str(plan.id),
        )

    @staticmethod
    def _snapshot_reference_id(payload: JsonObject) -> UUID:
        try:
            return UUID(cast(str, payload["snapshot_reference_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError(
                "Planning step is missing its frozen Context Snapshot."
            ) from exc


class VerifyPlanSafetyHandler:
    step_type = StepType.VERIFY_PLAN_SAFETY
    version = "phase-2a-verify-safety-v1"

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

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        plan_id = self._plan_id(context.claim.step.input_payload)
        plan = await self._plans.get_for_user(plan_id, context.claim.run.user_id)
        profile = await self._profiles.get_by_user_id(context.claim.run.user_id)
        if plan is None or profile is None:
            raise PermanentStepError("Generated plan context is no longer available.")
        constraints = await self._profiles.list_constraints(profile.id)
        catalog = {item.id: item for item in await self._exercises.list_active()}
        validation = self._safety.validate_plan(
            profile=profile,
            constraints=constraints,
            plan=plan,
            exercise_catalog=catalog,
        )
        if not validation.passed:
            raise PermanentStepError("Final safety validation rejected the plan.")
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=dict(context.claim.step.input_payload),
            result_reference=str(plan.id),
        )

    @staticmethod
    def _plan_id(payload: JsonObject) -> UUID:
        try:
            return UUID(cast(str, payload["plan_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError("Step is missing a valid plan reference.") from exc


class WaitForUserConfirmationHandler:
    step_type = StepType.WAIT_FOR_USER_CONFIRMATION
    version = "phase-2a-wait-confirmation-v1"

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        return StepExecutionResult(
            outcome=StepOutcome.WAITING_USER,
            output_payload=dict(context.claim.step.input_payload),
            result_reference=context.claim.run.result_reference,
        )


class FinalizeRunHandler:
    step_type = StepType.FINALIZE_RUN
    version = "phase-2a-finalize-v1"

    def __init__(self, *, plans: PlanRepository) -> None:
        self._plans = plans

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        try:
            plan_id = UUID(cast(str, context.claim.step.input_payload["plan_id"]))
            expected_revision = cast(int, context.claim.step.input_payload["revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError("Finalization input is invalid.") from exc
        plan = await self._plans.get_for_user(plan_id, context.claim.run.user_id)
        if (
            plan is None
            or plan.status is not WeeklyPlanStatus.CONFIRMED
            or plan.revision != expected_revision
        ):
            raise PermanentStepError(
                "The expected plan revision has not been confirmed."
            )
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=dict(context.claim.step.input_payload),
            result_reference=str(plan.id),
        )
