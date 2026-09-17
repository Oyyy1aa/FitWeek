"""Control plane for Schedule Plan integration Runs."""

import hashlib
import json
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from app.application.errors import (
    OrchestratorDisabled,
    RunAlreadyTerminal,
    RunIdempotencyConflict,
    RunNotFound,
    RunNotWaitingConfirmation,
    SchedulePlanVersionConflict,
)
from app.application.local_replanning import LocalReplanningService
from app.domain.orchestration.enums import (
    AgentStepStatus,
    PlanningRunStatus,
    StepType,
    WorkflowType,
)
from app.domain.orchestration.errors import (
    CheckpointConflict,
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
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.scheduling.repositories import ScheduleDraftRepository
from app.domain.users.models import UserAccount
from app.orchestration.clock import Clock
from app.orchestration.retry_policy import RetryPolicy


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleApplicationRunCreation:
    run: PlanningRun
    created: bool


class ScheduleApplicationRunService:
    def __init__(
        self,
        *,
        repository: OrchestrationRepository,
        drafts: ScheduleDraftRepository,
        revisions: LocalReplanningService,
        clock: Clock,
        retry_policy: RetryPolicy,
        enabled: bool,
    ) -> None:
        self._repository = repository
        self._drafts = drafts
        self._revisions = revisions
        self._clock = clock
        self._retry_policy = retry_policy
        self._enabled = enabled

    async def create_run(
        self,
        user: UserAccount,
        *,
        client_request_id: str,
        draft_id: UUID,
        expected_draft_version: int,
        root_plan_id: UUID,
        source_revision: int,
        expected_plan_version: int,
    ) -> ScheduleApplicationRunCreation:
        self._require_enabled()
        request_id = client_request_id.strip()
        if not request_id:
            raise RunIdempotencyConflict("client_request_id must not be blank.")
        if await self._drafts.get_draft(user.id, draft_id) is None:
            raise RunNotFound("Schedule Draft was not found for this user.")
        run_id = uuid5(NAMESPACE_URL, f"fitweek:schedule-run:{user.id}:{request_id}")
        payload: JsonObject = {
            "draft_id": str(draft_id),
            "expected_draft_version": expected_draft_version,
            "root_plan_id": str(root_plan_id),
            "source_revision": source_revision,
            "expected_plan_version": expected_plan_version,
            "apply_client_request_id": f"schedule-run:{run_id}",
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        now = self._clock.now()
        run = PlanningRun(
            id=run_id,
            user_id=user.id,
            workflow_type=WorkflowType.SCHEDULE_PLAN_INTEGRATION,
            status=PlanningRunStatus.CREATED,
            request_fingerprint=fingerprint,
            input_payload=payload,
            result_reference=None,
            current_step_id=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            version=1,
            client_request_id=request_id,
        )
        step = AgentStep(
            id=uuid5(NAMESPACE_URL, f"fitweek:schedule-run-step:{run.id}:1"),
            run_id=run.id,
            step_type=StepType.LOAD_SCHEDULE_APPLICATION_CONTEXT,
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
        return ScheduleApplicationRunCreation(run=result.run, created=result.created)

    async def get_run(self, user: UserAccount, run_id: UUID) -> PlanningRun:
        run = await self._repository.get_run_for_user(run_id, user.id)
        if (
            run is None
            or run.workflow_type is not WorkflowType.SCHEDULE_PLAN_INTEGRATION
        ):
            raise RunNotFound("Schedule application Run was not found.")
        return run

    async def list_steps(self, user: UserAccount, run_id: UUID) -> list[AgentStep]:
        return await self._repository.list_steps((await self.get_run(user, run_id)).id)

    async def list_checkpoints(
        self, user: UserAccount, run_id: UUID
    ) -> list[StepCheckpoint]:
        return await self._repository.list_checkpoints(
            (await self.get_run(user, run_id)).id
        )

    async def list_audit(
        self, user: UserAccount, run_id: UUID
    ) -> list[OrchestrationAuditEvent]:
        return await self._repository.list_audit_events(
            (await self.get_run(user, run_id)).id
        )

    async def confirm_run(
        self,
        user: UserAccount,
        run_id: UUID,
        *,
        expected_revision: int,
        expected_plan_version: int,
    ) -> PlanningRun:
        self._require_enabled()
        run = await self.get_run(user, run_id)
        if run.status is PlanningRunStatus.COMPLETED:
            return run
        if run.status not in {
            PlanningRunStatus.WAITING_CONFIRMATION,
            PlanningRunStatus.FINALIZING_SCHEDULE_APPLICATION,
        }:
            raise RunNotWaitingConfirmation(
                "Schedule application Run is not waiting for confirmation."
            )
        waiting = await self._waiting_step(run.id)
        payload = dict(waiting.output_payload or waiting.input_payload)
        created_revision = int(str(payload.get("created_revision", 0)))
        resulting_version = int(str(payload.get("resulting_plan_version", 0)))
        if (
            expected_revision != created_revision
            or expected_plan_version != resulting_version
        ):
            raise SchedulePlanVersionConflict("Schedule confirmation is stale.")
        root_plan_id = UUID(str(payload["root_plan_id"]))
        plan = await self._revisions.get_revision(user, root_plan_id, created_revision)
        if plan.status is WeeklyPlanStatus.VALIDATED:
            plan = await self._revisions.confirm_revision(
                user,
                root_plan_id,
                created_revision,
                expected_version=expected_plan_version,
            )
        resume_payload: JsonObject = {
            **payload,
            "confirmed_plan_version": plan.version,
        }
        try:
            await self._repository.resume_waiting_step(
                run_id=run.id,
                expected_step_id=waiting.id,
                handler_version="phase-6b-user-schedule-confirmation-v1",
                next_step_type=StepType.FINALIZE_SCHEDULE_APPLICATION,
                now=self._clock.now(),
                resume_payload=resume_payload,
            )
        except CheckpointConflict as exc:
            raise SchedulePlanVersionConflict(str(exc)) from exc
        return await self.get_run(user, run.id)

    async def cancel_run(self, user: UserAccount, run_id: UUID) -> PlanningRun:
        self._require_enabled()
        run = await self.get_run(user, run_id)
        if run.status is PlanningRunStatus.CANCELLED:
            return run
        try:
            return await self._repository.cancel_run(
                run_id=run.id, now=self._clock.now()
            )
        except InvalidRunStateTransition as exc:
            raise RunAlreadyTerminal(str(exc)) from exc

    async def _waiting_step(self, run_id: UUID) -> AgentStep:
        matches = [
            item
            for item in await self._repository.list_steps(run_id)
            if item.step_type is StepType.WAIT_FOR_SCHEDULE_REVISION_CONFIRMATION
            and item.status in {AgentStepStatus.WAITING_USER, AgentStepStatus.SUCCEEDED}
        ]
        if len(matches) != 1:
            raise RunNotWaitingConfirmation("Run has no Schedule confirmation step.")
        return matches[0]

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise OrchestratorDisabled("The in-memory orchestrator is disabled.")
