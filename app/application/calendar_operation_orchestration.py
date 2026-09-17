"""Control plane for approved Calendar operation Runs."""

import hashlib
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from app.application.calendar_operations import CalendarOperationService
from app.application.errors import (
    OrchestratorDisabled,
    RunAlreadyTerminal,
    RunIdempotencyConflict,
    RunNotFound,
)
from app.domain.calendar_operations.enums import CalendarOperationDraftStatus
from app.domain.orchestration.enums import (
    AgentStepStatus,
    PlanningRunStatus,
    StepType,
    WorkflowType,
)
from app.domain.orchestration.errors import InvalidRunStateTransition
from app.domain.orchestration.errors import RunIdempotencyConflict as DomainConflict
from app.domain.orchestration.models import (
    AgentStep,
    JsonObject,
    OrchestrationAuditEvent,
    PlanningRun,
    StepCheckpoint,
)
from app.domain.orchestration.repositories import OrchestrationRepository
from app.domain.users.models import UserAccount
from app.orchestration.clock import Clock
from app.orchestration.retry_policy import RetryPolicy


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarOperationRunCreation:
    run: PlanningRun
    created: bool


class CalendarOperationRunService:
    def __init__(
        self,
        *,
        repository: OrchestrationRepository,
        operations: CalendarOperationService,
        clock: Clock,
        retry_policy: RetryPolicy,
        enabled: bool,
    ) -> None:
        self._repository = repository
        self._operations = operations
        self._clock = clock
        self._retry_policy = retry_policy
        self._enabled = enabled

    async def create_run(
        self, user: UserAccount, *, client_request_id: str, draft_id: UUID
    ) -> CalendarOperationRunCreation:
        self._require_enabled()
        draft = await self._operations.get_draft(user, draft_id)
        if draft.status not in {
            CalendarOperationDraftStatus.APPROVED,
            CalendarOperationDraftStatus.PARTIALLY_SUCCEEDED,
        }:
            raise RunIdempotencyConflict("Calendar Draft has not been approved.")
        request_id = client_request_id.strip()
        if not request_id:
            raise RunIdempotencyConflict("client_request_id must not be blank.")
        run_id = uuid5(NAMESPACE_URL, f"fitweek:calendar-run:{user.id}:{request_id}")
        payload: JsonObject = {"draft_id": str(draft.id)}
        now = self._clock.now()
        run = PlanningRun(
            id=run_id,
            user_id=user.id,
            workflow_type=WorkflowType.CALENDAR_OPERATION_EXECUTION,
            status=PlanningRunStatus.CREATED,
            request_fingerprint=hashlib.sha256(
                f"{user.id}:{draft.id}:{draft.version}".encode()
            ).hexdigest(),
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
            id=uuid5(NAMESPACE_URL, f"fitweek:calendar-run-step:{run.id}:1"),
            run_id=run.id,
            step_type=StepType.LOAD_CALENDAR_OPERATION_DRAFT,
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
            result = await self._repository.create_run_with_initial_steps(run, (step,))
        except DomainConflict as exc:
            raise RunIdempotencyConflict(str(exc)) from exc
        return CalendarOperationRunCreation(run=result.run, created=result.created)

    async def get_run(self, user: UserAccount, run_id: UUID) -> PlanningRun:
        run = await self._repository.get_run_for_user(run_id, user.id)
        if (
            run is None
            or run.workflow_type is not WorkflowType.CALENDAR_OPERATION_EXECUTION
        ):
            raise RunNotFound("Calendar operation Run was not found.")
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

    async def retry_run(self, user: UserAccount, run_id: UUID) -> PlanningRun:
        run = await self.get_run(user, run_id)
        if run.status not in {
            PlanningRunStatus.FAILED_RETRYABLE,
            PlanningRunStatus.EXECUTING_CALENDAR_OPERATION,
            PlanningRunStatus.VERIFYING_CALENDAR_OPERATION,
        }:
            return run
        return run

    async def cancel_run(self, user: UserAccount, run_id: UUID) -> PlanningRun:
        run = await self.get_run(user, run_id)
        if run.status is PlanningRunStatus.CANCELLED:
            return run
        try:
            return await self._repository.cancel_run(
                run_id=run.id, now=self._clock.now()
            )
        except InvalidRunStateTransition as exc:
            raise RunAlreadyTerminal(str(exc)) from exc

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise OrchestratorDisabled("The in-memory orchestrator is disabled.")
