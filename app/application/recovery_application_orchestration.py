"""Control plane for deterministic Recovery Plan application Runs."""

import hashlib
import json
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from app.application.errors import (
    OrchestratorDisabled,
    RecoveryDraftNotAccepted,
    RecoveryDraftNotFound,
    RecoveryPlanVersionConflict,
    RecoverySubdraftRejected,
    RecoverySubdraftReviewRequired,
    RunAlreadyTerminal,
    RunIdempotencyConflict,
    RunNotFound,
    RunNotWaitingConfirmation,
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
from app.domain.recovery.enums import RecoveryDraftStatus
from app.domain.recovery.repositories import RecoveryDraftRepository
from app.domain.scheduling.enums import ScheduleDraftOutcome, ScheduleDraftStatus
from app.domain.scheduling.repositories import ScheduleDraftRepository
from app.domain.session_design.enums import SessionDesignDraftStatus
from app.domain.session_design.repositories import SessionDesignRepository
from app.domain.users.models import UserAccount
from app.orchestration.clock import Clock
from app.orchestration.retry_policy import RetryPolicy


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryApplicationRunCreation:
    run: PlanningRun
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryRunSubdrafts:
    session_design_draft_ids: tuple[UUID, ...]
    schedule_draft_ids: tuple[UUID, ...]


class RecoveryApplicationRunService:
    def __init__(
        self,
        *,
        repository: OrchestrationRepository,
        drafts: RecoveryDraftRepository,
        session_designs: SessionDesignRepository,
        schedules: ScheduleDraftRepository,
        revisions: LocalReplanningService,
        clock: Clock,
        retry_policy: RetryPolicy,
        enabled: bool,
    ) -> None:
        self._repository = repository
        self._drafts = drafts
        self._session_designs = session_designs
        self._schedules = schedules
        self._revisions = revisions
        self._clock = clock
        self._retry_policy = retry_policy
        self._enabled = enabled

    async def create_run(
        self,
        user: UserAccount,
        *,
        client_request_id: str,
        recovery_draft_id: UUID,
        expected_draft_version: int,
        root_plan_id: UUID,
        source_revision: int,
        expected_plan_version: int,
    ) -> RecoveryApplicationRunCreation:
        self._require_enabled()
        request_id = client_request_id.strip()
        if not request_id:
            raise RunIdempotencyConflict("client_request_id must not be blank.")
        draft = await self._drafts.get_draft(user.id, recovery_draft_id)
        if draft is None:
            raise RecoveryDraftNotFound("Recovery Draft was not found.")
        run_id = uuid5(NAMESPACE_URL, f"fitweek:recovery-run:{user.id}:{request_id}")
        payload: JsonObject = {
            "recovery_draft_id": str(recovery_draft_id),
            "expected_draft_version": expected_draft_version,
            "root_plan_id": str(root_plan_id),
            "source_revision": source_revision,
            "expected_plan_version": expected_plan_version,
            "selected_action_candidate_ids": [
                str(item) for item in draft.selected_action_candidate_ids
            ],
            "apply_client_request_id": f"recovery-run:{run_id}",
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing = await self._repository.get_run_for_user(run_id, user.id)
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise RunIdempotencyConflict(
                    "The Recovery Run request ID was reused with another payload."
                )
            return RecoveryApplicationRunCreation(run=existing, created=False)
        if draft.status is not RecoveryDraftStatus.ACCEPTED:
            raise RecoveryDraftNotAccepted("Only an accepted Recovery Draft can run.")
        now = self._clock.now()
        run = PlanningRun(
            id=run_id,
            user_id=user.id,
            workflow_type=WorkflowType.RECOVERY_PLAN_INTEGRATION,
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
            id=uuid5(NAMESPACE_URL, f"fitweek:recovery-run-step:{run.id}:1"),
            run_id=run.id,
            step_type=StepType.LOAD_RECOVERY_APPLICATION_CONTEXT,
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
        return RecoveryApplicationRunCreation(run=result.run, created=result.created)

    async def get_run(self, user: UserAccount, run_id: UUID) -> PlanningRun:
        run = await self._repository.get_run_for_user(run_id, user.id)
        if (
            run is None
            or run.workflow_type is not WorkflowType.RECOVERY_PLAN_INTEGRATION
        ):
            raise RunNotFound("Recovery application Run was not found.")
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

    async def subdrafts(self, user: UserAccount, run_id: UUID) -> RecoveryRunSubdrafts:
        steps = await self.list_steps(user, run_id)
        created = next(
            (
                item
                for item in reversed(steps)
                if item.step_type is StepType.CREATE_RECOVERY_SUBDRAFTS
                and item.output_payload is not None
            ),
            None,
        )
        payload: JsonObject = (
            created.output_payload
            if created is not None and created.output_payload is not None
            else {}
        )
        design_values = payload.get("session_design_draft_ids", [])
        schedule_values = payload.get("schedule_draft_ids", [])
        if not isinstance(design_values, list) or not isinstance(schedule_values, list):
            raise RecoverySubdraftReviewRequired("Child Draft references are invalid.")
        return RecoveryRunSubdrafts(
            session_design_draft_ids=tuple(UUID(str(item)) for item in design_values),
            schedule_draft_ids=tuple(UUID(str(item)) for item in schedule_values),
        )

    async def continue_run(self, user: UserAccount, run_id: UUID) -> PlanningRun:
        self._require_enabled()
        run = await self.get_run(user, run_id)
        if run.status is not PlanningRunStatus.WAITING_SUBDRAFT_REVIEW:
            raise RecoverySubdraftReviewRequired(
                "Recovery Run is not waiting for child Draft review."
            )
        waiting = await self._waiting_step(
            run.id, StepType.WAIT_FOR_RECOVERY_SUBDRAFT_REVIEWS
        )
        payload = dict(waiting.output_payload or waiting.input_payload)
        try:
            await self._require_children_accepted(user, payload)
        except RecoverySubdraftRejected:
            await self._repository.cancel_run(run_id=run.id, now=self._clock.now())
            raise
        try:
            await self._repository.resume_waiting_step(
                run_id=run.id,
                expected_step_id=waiting.id,
                handler_version="phase-7b-user-subdraft-review-v1",
                next_step_type=StepType.BUILD_RECOVERY_PLAN_REVISION,
                now=self._clock.now(),
                resume_payload=payload,
            )
        except CheckpointConflict as exc:
            raise RecoverySubdraftReviewRequired(str(exc)) from exc
        return await self.get_run(user, run.id)

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
            PlanningRunStatus.FINALIZING_RECOVERY_APPLICATION,
        }:
            raise RunNotWaitingConfirmation(
                "Recovery Run is not waiting for Revision confirmation."
            )
        waiting = await self._waiting_step(
            run.id, StepType.WAIT_FOR_RECOVERY_REVISION_CONFIRMATION
        )
        payload = dict(waiting.output_payload or waiting.input_payload)
        created_revision = int(str(payload.get("created_revision", 0)))
        resulting_version = int(str(payload.get("resulting_plan_version", 0)))
        if (
            expected_revision != created_revision
            or expected_plan_version != resulting_version
        ):
            raise RecoveryPlanVersionConflict("Recovery confirmation is stale.")
        plan = await self._revisions.get_revision(
            user, UUID(str(payload["root_plan_id"])), created_revision
        )
        if plan.status is WeeklyPlanStatus.VALIDATED:
            plan = await self._revisions.confirm_revision(
                user,
                UUID(str(payload["root_plan_id"])),
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
                handler_version="phase-7b-user-recovery-confirmation-v1",
                next_step_type=StepType.FINALIZE_RECOVERY_APPLICATION,
                now=self._clock.now(),
                resume_payload=resume_payload,
            )
        except CheckpointConflict as exc:
            raise RecoveryPlanVersionConflict(str(exc)) from exc
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

    async def _require_children_accepted(
        self, user: UserAccount, payload: JsonObject
    ) -> None:
        design_values = payload.get("session_design_draft_ids", [])
        schedule_values = payload.get("schedule_draft_ids", [])
        if not isinstance(design_values, list) or not isinstance(schedule_values, list):
            raise RecoverySubdraftReviewRequired("Child Draft references are invalid.")
        for value in design_values:
            design = await self._session_designs.get_draft(user.id, UUID(str(value)))
            if design is None:
                raise RecoverySubdraftReviewRequired(
                    "Session Design child Draft is missing."
                )
            if design.status in {
                SessionDesignDraftStatus.REJECTED,
                SessionDesignDraftStatus.EXPIRED,
            }:
                raise RecoverySubdraftRejected(
                    "Session Design child Draft was rejected or expired."
                )
            if design.status not in {
                SessionDesignDraftStatus.ACCEPTED,
                SessionDesignDraftStatus.APPLIED,
            }:
                raise RecoverySubdraftReviewRequired(
                    "Every Session Design child Draft must be accepted."
                )
        for value in schedule_values:
            schedule = await self._schedules.get_draft(user.id, UUID(str(value)))
            if schedule is None:
                raise RecoverySubdraftReviewRequired("Schedule child Draft is missing.")
            if schedule.status in {
                ScheduleDraftStatus.REJECTED,
                ScheduleDraftStatus.EXPIRED,
            }:
                raise RecoverySubdraftRejected(
                    "Schedule child Draft was rejected or expired."
                )
            if (
                schedule.status
                not in {ScheduleDraftStatus.ACCEPTED, ScheduleDraftStatus.APPLIED}
                or schedule.outcome is not ScheduleDraftOutcome.COMPLETE
            ):
                raise RecoverySubdraftReviewRequired(
                    "Every Schedule child Draft must be complete and accepted."
                )

    async def _waiting_step(self, run_id: UUID, step_type: StepType) -> AgentStep:
        matches = [
            item
            for item in await self._repository.list_steps(run_id)
            if item.step_type is step_type
            and item.status in {AgentStepStatus.WAITING_USER, AgentStepStatus.SUCCEEDED}
        ]
        if len(matches) != 1:
            raise RunNotWaitingConfirmation("Run has no expected waiting step.")
        return matches[0]

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise OrchestratorDisabled("The in-memory orchestrator is disabled.")
