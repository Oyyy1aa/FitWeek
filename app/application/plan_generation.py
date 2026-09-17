"""Use case for deterministic, repository-backed weekly-plan generation."""

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID, uuid4

from app.application.contexts import ContextApplicationService
from app.application.errors import (
    ConflictError,
    PlanGenerationFailed,
    PlanGenerationIdempotencyConflict,
    ResourceNotFound,
)
from app.domain.common import RepositoryError
from app.domain.context.enums import (
    AgentType,
    ContextDegradedMode,
    ContextSectionName,
)
from app.domain.context.models import ContextBuildCommand, FrozenContextSnapshot
from app.domain.exercises.models import Exercise
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.planning.models import (
    GenerateWeeklyPlanCommand,
    GenerationFailureReason,
    GenerationMetadata,
    PlanGenerationError,
)
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.domain.profiles.repositories import ProfileRepository
from app.domain.tools.enums import ToolCaller, ToolId, ToolInvocationStatus
from app.domain.tools.models import ToolInvocationContext
from app.domain.users.models import UserAccount
from app.memory.metrics import MemoryMetrics
from app.planning.context_preferences import PlanningContextPreferences
from app.planning.generator import DeterministicPlanGenerator
from app.planning.repair import PlanRepairer
from app.safety.engine import SafetyEngine
from app.safety.models import SafetyValidationResult
from app.tool_adapters.contracts import (
    ExerciseCatalogSearchRequest,
    ExerciseCatalogSearchResponse,
)
from app.tool_gateway.gateway import ToolGateway


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanGenerationResult:
    plan: WeeklyPlan
    validation: SafetyValidationResult
    metadata: GenerationMetadata
    repair_attempts: int


class PlanGenerationService:
    """Load inputs, generate, safety-check, repair at most twice, then save."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        exercises: ExerciseRepository,
        plans: PlanRepository,
        safety_engine: SafetyEngine,
        generator: DeterministicPlanGenerator | None = None,
        repairer: PlanRepairer | None = None,
        contexts: ContextApplicationService | None = None,
        memory_metrics: MemoryMetrics | None = None,
        tool_gateway: ToolGateway | None = None,
    ) -> None:
        self._profiles = profiles
        self._exercises = exercises
        self._plans = plans
        self._safety = safety_engine
        self._generator = generator or DeterministicPlanGenerator()
        self._repairer = repairer or PlanRepairer(self._generator)
        self._contexts = contexts
        self._memory_metrics = memory_metrics
        self._tool_gateway = tool_gateway

    def attach_tool_gateway(self, gateway: ToolGateway) -> None:
        self._tool_gateway = gateway

    async def generate_plan(
        self,
        user: UserAccount,
        command: GenerateWeeklyPlanCommand,
        *,
        snapshot_reference_id: UUID | None = None,
        context_scope_id: str | None = None,
        run_id: UUID | None = None,
        step_id: UUID | None = None,
    ) -> PlanGenerationResult:
        profile = await self._profiles.get_by_user_id(user.id)
        if profile is None:
            raise ResourceNotFound(
                "Create a fitness profile before generating a weekly plan."
            )
        constraints = tuple(await self._profiles.list_constraints(profile.id))
        catalog = await self._load_catalog(user)
        snapshot = await self._planning_context(
            user=user,
            command=command,
            catalog=catalog,
            snapshot_reference_id=snapshot_reference_id,
            context_scope_id=context_scope_id,
            run_id=run_id,
            step_id=step_id,
        )
        preferences = await self._context_preferences(snapshot)
        included_count = len(snapshot.reference.memory_versions) if snapshot else 0
        shadowed_count = len(snapshot.context.conflicts) if snapshot else 0
        try:
            candidate = self._generator.generate_candidate(
                user_id=user.id,
                profile=profile,
                constraints=constraints,
                catalog=catalog,
                command=command,
                context_preferences=preferences,
                context_snapshot_reference_id=(
                    snapshot.reference.id if snapshot else None
                ),
                context_fingerprint=(
                    snapshot.reference.context_fingerprint if snapshot else "none"
                ),
                context_contract_version=(
                    snapshot.reference.contract_version if snapshot else "none"
                ),
                context_policy_version=(
                    snapshot.reference.policy_version if snapshot else "none"
                ),
                context_degradation_state=(
                    snapshot.reference.degraded_mode.value if snapshot else "NONE"
                ),
                included_memory_count=included_count,
                shadowed_memory_count=shadowed_count,
            )
        except PlanGenerationError as exc:
            raise PlanGenerationFailed(exc.reasons) from exc

        if command.client_request_id is not None:
            request = await self._plans.get_generation_request(
                user.id, command.client_request_id
            )
            if request is not None:
                old_fingerprint, old_plan = request
                if old_fingerprint != candidate.metadata.input_fingerprint:
                    raise PlanGenerationIdempotencyConflict(
                        "The request ID is bound to a different effective Context."
                    )
                return self._existing_result(
                    existing=old_plan,
                    expected_metadata=candidate.metadata,
                    profile=profile,
                    constraints=constraints,
                    catalog={item.id: item for item in catalog},
                )

        catalog_map = {item.id: item for item in catalog}
        existing = await self._plans.get_for_user(candidate.plan.id, user.id)
        if existing is not None:
            return self._existing_result(
                existing=existing,
                expected_metadata=candidate.metadata,
                profile=profile,
                constraints=constraints,
                catalog=catalog_map,
            )

        plan = candidate.plan
        repair_attempts = 0
        validation = self._validate(
            profile=profile,
            constraints=constraints,
            plan=plan,
            catalog=catalog_map,
        )
        while not validation.passed:
            if repair_attempts >= self._generator.policy.maximum_repair_attempts:
                break
            repair_attempts += 1
            try:
                repaired = self._repairer.repair(
                    plan=plan,
                    violations=validation.violations,
                    profile=profile,
                    constraints=constraints,
                    catalog=catalog,
                    command=command,
                    attempt=repair_attempts,
                    context_preferences=preferences,
                    metadata=candidate.metadata,
                )
            except PlanGenerationError as exc:
                raise PlanGenerationFailed(exc.reasons) from exc
            if repaired is None:
                break
            plan = repaired
            validation = self._validate(
                profile=profile,
                constraints=constraints,
                plan=plan,
                catalog=catalog_map,
            )

        if not validation.passed:
            raise PlanGenerationFailed(
                tuple(
                    GenerationFailureReason(
                        code=violation.code,
                        message=violation.message,
                    )
                    for violation in validation.violations
                )
            )

        validated = replace(plan, status=WeeklyPlanStatus.VALIDATED)
        try:
            saved = await self._plans.save(validated)
            if command.client_request_id is not None:
                await self._plans.bind_generation_request(
                    user.id,
                    command.client_request_id,
                    candidate.metadata.input_fingerprint,
                    saved.id,
                )
        except RepositoryError as exc:
            raise ConflictError(
                "Generated plan identity or revision conflicts with stored data."
            ) from exc
        return PlanGenerationResult(
            plan=saved,
            validation=validation,
            metadata=candidate.metadata,
            repair_attempts=repair_attempts,
        )

    async def _planning_context(
        self,
        *,
        user: UserAccount,
        command: GenerateWeeklyPlanCommand,
        catalog: tuple[Exercise, ...],
        snapshot_reference_id: UUID | None,
        context_scope_id: str | None,
        run_id: UUID | None,
        step_id: UUID | None,
    ) -> FrozenContextSnapshot | None:
        if self._contexts is None:
            return None
        if snapshot_reference_id is not None:
            return await self._contexts.get_snapshot(user, snapshot_reference_id)
        task = self._current_task(command)
        catalog_reference = hashlib.sha256(
            "|".join(
                f"{item.id}:{item.version}"
                for item in sorted(catalog, key=lambda x: x.id)
            ).encode("utf-8")
        ).hexdigest()
        scope = context_scope_id or (
            f"plan-direct:{user.id}:"
            f"{command.client_request_id or self._command_fingerprint(command)}"
        )
        snapshot = await self._contexts.build_snapshot(
            user,
            ContextBuildCommand(
                agent_type=AgentType.PLAN_GENERATION,
                current_task=task,
                catalog_reference=catalog_reference,
                run_id=run_id,
                step_id=step_id,
            ),
            scope_id=scope,
        )
        if self._memory_metrics is not None:
            self._memory_metrics.increment("plan_generation_context_builds")
            count = len(snapshot.reference.memory_versions)
            if count:
                self._memory_metrics.increment(
                    "memories_injected_into_plan_generation", count
                )
            if snapshot.reference.degraded_mode is ContextDegradedMode.NO_MEMORY:
                self._memory_metrics.increment("plan_generation_no_memory_degraded")
        return snapshot

    async def _context_preferences(
        self, snapshot: FrozenContextSnapshot | None
    ) -> PlanningContextPreferences:
        if snapshot is None:
            return PlanningContextPreferences()
        values: dict[str, list[str]] = {
            "preferred_location": [],
            "preferred_time_of_day": [],
            "preferred_equipment": [],
            "disliked_activity": [],
            "training_style": [],
        }
        for section in snapshot.context.sections:
            if section.name is not ContextSectionName.RELEVANT_CONFIRMED_MEMORIES:
                continue
            for item in section.items:
                if item.key in values:
                    values[item.key].append(item.value)
        return PlanningContextPreferences(
            preferred_locations=tuple(values["preferred_location"]),
            preferred_times_of_day=tuple(values["preferred_time_of_day"]),
            preferred_equipment=tuple(values["preferred_equipment"]),
            disliked_activities=tuple(values["disliked_activity"]),
            training_styles=tuple(values["training_style"]),
        )

    @staticmethod
    def _current_task(command: GenerateWeeklyPlanCommand) -> dict[str, str]:
        task = {
            "request_type": "deterministic_plan_generation",
            "week_start": command.week_start.isoformat(),
            "availability_fingerprint": PlanGenerationService._command_fingerprint(
                command
            ),
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
        return task

    @staticmethod
    def _command_fingerprint(command: GenerateWeeklyPlanCommand) -> str:
        payload = {
            "week_start": command.week_start.isoformat(),
            "availability": [
                (
                    slot.start_utc.isoformat(),
                    slot.end_utc.isoformat(),
                    slot.location_type.value,
                )
                for slot in command.availability_slots
            ],
            "preferred_locations": [item.value for item in command.preferred_locations],
            "preferred_session_types": [
                item.value for item in command.preferred_session_types
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _existing_result(
        self,
        *,
        existing: WeeklyPlan,
        expected_metadata: GenerationMetadata,
        profile: FitnessProfile,
        constraints: tuple[UserConstraint, ...],
        catalog: dict[str, Exercise],
    ) -> PlanGenerationResult:
        if (
            existing.generation_metadata is None
            or existing.generation_metadata.get("input_fingerprint")
            != expected_metadata.input_fingerprint
        ):
            raise ConflictError(
                "A different plan already owns this generation identity."
            )
        validation = self._safety.validate_plan(
            profile=profile,
            constraints=constraints,
            plan=existing,
            exercise_catalog=catalog,
        )
        if not validation.passed:
            raise PlanGenerationFailed(
                tuple(
                    GenerationFailureReason(
                        code=item.code,
                        message=item.message,
                    )
                    for item in validation.violations
                )
            )
        return PlanGenerationResult(
            plan=existing,
            validation=validation,
            metadata=GenerationMetadata.from_dict(existing.generation_metadata),
            repair_attempts=0,
        )

    async def _load_catalog(self, user: UserAccount) -> tuple[Exercise, ...]:
        if self._tool_gateway is None:
            return tuple(await self._exercises.list_active())
        now = self._tool_gateway.clock.now()
        outcome = await self._tool_gateway.invoke(
            ToolInvocationContext(
                invocation_id=uuid4(),
                correlation_id=uuid4(),
                user_id=user.id,
                caller=ToolCaller.PLAN_GENERATION_APPLICATION,
                tool_id=ToolId.EXERCISE_CATALOG_SEARCH,
                tool_version="phase-8a-v1",
                deadline_at=now + timedelta(seconds=2),
                created_at=now,
            ),
            ExerciseCatalogSearchRequest(user_id=user.id),
        )
        if (
            outcome.result.status is not ToolInvocationStatus.SUCCEEDED
            or not isinstance(outcome.response, ExerciseCatalogSearchResponse)
        ):
            raise PlanGenerationFailed(
                (
                    GenerationFailureReason(
                        code="NO_ELIGIBLE_EXERCISES",
                        message="The controlled exercise catalog is unavailable.",
                    ),
                )
            )
        return tuple(item.to_domain() for item in outcome.response.exercises)

    def _validate(
        self,
        *,
        profile: FitnessProfile,
        constraints: tuple[UserConstraint, ...],
        plan: WeeklyPlan,
        catalog: dict[str, Exercise],
    ) -> SafetyValidationResult:
        return self._safety.validate_plan(
            profile=profile,
            constraints=constraints,
            plan=plan,
            exercise_catalog=catalog,
        )
