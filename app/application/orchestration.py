"""User-scoped orchestration use cases and confirmation compensation boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid5

from app.application.errors import (
    ConflictError,
    InvalidStateTransition,
    OrchestratorDisabled,
    RunAlreadyTerminal,
    RunConfirmationConflict,
    RunIdempotencyConflict,
    RunNotFound,
    RunNotWaitingConfirmation,
)
from app.application.plans import PlanService
from app.domain.orchestration.enums import (
    AgentStepStatus,
    PlanningRunStatus,
    StepType,
    WorkflowType,
)
from app.domain.orchestration.errors import (
    InvalidRunStateTransition,
)
from app.domain.orchestration.errors import (
    RunIdempotencyConflict as DomainRunIdempotencyConflict,
)
from app.domain.orchestration.models import (
    AgentStep,
    JsonObject,
    OrchestrationAuditEvent,
    PlanningRun,
    RunCreationResult,
    StepCheckpoint,
)
from app.domain.orchestration.repositories import OrchestrationRepository
from app.domain.planning.models import GenerateWeeklyPlanCommand
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.users.models import UserAccount
from app.orchestration.clock import Clock
from app.orchestration.metrics import OrchestratorMetrics, OrchestratorMetricsSnapshot
from app.orchestration.retry_policy import RetryPolicy

_RUN_ID_NAMESPACE = UUID("f8f1c71e-d8ae-4e40-b8ce-38ad4d11d538")
_INITIAL_STEP_NAMESPACE = UUID("dce3c8d1-2c08-49fc-b14d-e2db61ee56c5")


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanningRunCreation:
    run: PlanningRun
    created: bool


class OrchestrationService:
    def __init__(
        self,
        *,
        repository: OrchestrationRepository,
        plan_service: PlanService,
        clock: Clock,
        retry_policy: RetryPolicy,
        metrics: OrchestratorMetrics,
        enabled: bool,
    ) -> None:
        self._repository = repository
        self._plans = plan_service
        self._clock = clock
        self._retry_policy = retry_policy
        self._metrics = metrics
        self._enabled = enabled

    async def create_run(
        self,
        user: UserAccount,
        *,
        client_request_id: str,
        command: GenerateWeeklyPlanCommand,
    ) -> PlanningRunCreation:
        self._require_enabled()
        normalized_request_id = client_request_id.strip()
        if not normalized_request_id:
            raise RunIdempotencyConflict("client_request_id must not be blank.")
        payload = self._command_payload(command)
        canonical = json.dumps(
            {"user_id": str(user.id), "input": payload},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        run_id = uuid5(_RUN_ID_NAMESPACE, f"{user.id}:{normalized_request_id}")
        now = self._clock.now()
        run = PlanningRun(
            id=run_id,
            user_id=user.id,
            workflow_type=WorkflowType.DETERMINISTIC_PLAN_GENERATION,
            status=PlanningRunStatus.CREATED,
            request_fingerprint=fingerprint,
            input_payload=payload,
            result_reference=None,
            current_step_id=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            version=1,
            client_request_id=normalized_request_id,
        )
        step = AgentStep(
            id=uuid5(_INITIAL_STEP_NAMESPACE, f"{run_id}:1"),
            run_id=run_id,
            step_type=StepType.LOAD_PROFILE_CONTEXT,
            status=AgentStepStatus.READY,
            sequence_no=1,
            priority=100,
            input_payload={},
            output_payload=None,
            dependency_step_ids=(),
            attempt_count=0,
            max_attempts=self._retry_policy.max_attempts,
            next_execute_at=now,
            worker_id=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            last_error_code=None,
            last_error_message=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            version=1,
        )
        try:
            result: RunCreationResult = (
                await self._repository.create_run_with_initial_steps(run, (step,))
            )
        except DomainRunIdempotencyConflict as exc:
            raise RunIdempotencyConflict(str(exc)) from exc
        return PlanningRunCreation(run=result.run, created=result.created)

    async def get_run(self, user: UserAccount, run_id: UUID) -> PlanningRun:
        run = await self._repository.get_run_for_user(run_id, user.id)
        if run is None:
            raise RunNotFound("Planning run was not found.")
        return run

    async def list_steps(self, user: UserAccount, run_id: UUID) -> list[AgentStep]:
        await self.get_run(user, run_id)
        return await self._repository.list_steps(run_id)

    async def list_checkpoints(
        self,
        user: UserAccount,
        run_id: UUID,
    ) -> list[StepCheckpoint]:
        await self.get_run(user, run_id)
        return await self._repository.list_checkpoints(run_id)

    async def list_audit(
        self,
        user: UserAccount,
        run_id: UUID,
    ) -> list[OrchestrationAuditEvent]:
        await self.get_run(user, run_id)
        return await self._repository.list_audit_events(run_id)

    async def confirm_and_resume_run(
        self,
        user: UserAccount,
        run_id: UUID,
        *,
        expected_plan_version: int,
    ) -> PlanningRun:
        self._require_enabled()
        run = await self.get_run(user, run_id)
        if run.status not in {
            PlanningRunStatus.WAITING_CONFIRMATION,
            PlanningRunStatus.COMPLETED,
        }:
            raise RunNotWaitingConfirmation("Run is not waiting for plan confirmation.")
        steps = await self._repository.list_steps(run.id)
        waiting = [
            item
            for item in steps
            if item.step_type is StepType.WAIT_FOR_USER_CONFIRMATION
            and item.status in {AgentStepStatus.WAITING_USER, AgentStepStatus.SUCCEEDED}
        ]
        if len(waiting) != 1:
            raise RunNotWaitingConfirmation(
                "Run does not have one resumable confirmation step."
            )
        if run.result_reference is None:
            raise RunNotWaitingConfirmation("Run has no generated plan reference.")
        try:
            plan_id = UUID(run.result_reference)
        except ValueError as exc:
            raise RunNotWaitingConfirmation("Run result reference is invalid.") from exc

        plan = await self._plans.get_plan(user, plan_id)
        if plan.status is WeeklyPlanStatus.CONFIRMED:
            if expected_plan_version != plan.version - 1:
                raise RunConfirmationConflict(
                    "Confirmation payload differs from the completed request."
                )
        else:
            try:
                await self._plans.confirm_plan(
                    user,
                    plan_id,
                    expected_version=expected_plan_version,
                )
            except (ConflictError, InvalidStateTransition) as exc:
                raise RunConfirmationConflict(str(exc)) from exc

        if waiting[0].status is AgentStepStatus.WAITING_USER:
            await self._repository.resume_waiting_step(
                run_id=run.id,
                expected_step_id=waiting[0].id,
                handler_version="phase-2a-user-confirmation-v1",
                next_step_type=StepType.FINALIZE_RUN,
                now=self._clock.now(),
            )
        return await self.get_run(user, run.id)

    async def cancel_run(self, user: UserAccount, run_id: UUID) -> PlanningRun:
        self._require_enabled()
        run = await self.get_run(user, run_id)
        try:
            return await self._repository.cancel_run(
                run_id=run.id,
                now=self._clock.now(),
            )
        except InvalidRunStateTransition as exc:
            raise RunAlreadyTerminal(str(exc)) from exc

    def metrics(self) -> OrchestratorMetricsSnapshot:
        return self._metrics.snapshot()

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise OrchestratorDisabled("The in-memory orchestrator is disabled.")

    @staticmethod
    def _command_payload(command: GenerateWeeklyPlanCommand) -> JsonObject:
        return {
            "week_start": command.week_start.isoformat(),
            "availability_slots": [
                {
                    "start": slot.start.isoformat(),
                    "end": slot.end.isoformat(),
                    "location_type": slot.location_type.value,
                }
                for slot in command.availability_slots
            ],
            "preferred_locations": [item.value for item in command.preferred_locations],
            "preferred_session_types": [
                item.value for item in command.preferred_session_types
            ],
        }
