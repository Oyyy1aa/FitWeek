"""Profile Agent review Run creation, explicit resume, and rejection use cases."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid5

from app.application.errors import (
    OrchestratorDisabled,
    ProfileDraftApplyIdempotencyConflict,
    ProfileRunNotWaitingReview,
    RunAlreadyTerminal,
    RunIdempotencyConflict,
    RunNotFound,
)
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
from app.domain.profile_agent.apply_models import (
    ProfileDraftApplyDecision,
    ProfileDraftRejectResult,
)
from app.domain.users.models import UserAccount
from app.orchestration.clock import Clock
from app.orchestration.retry_policy import RetryPolicy
from app.profile_application.apply import ProfileDraftApplyService
from app.profile_application.decision_codec import decision_to_payload

_PROFILE_RUN_ID_NAMESPACE = UUID("43ed1fa2-d1a2-4cb5-a3cf-9021c7d014e8")
_PROFILE_INITIAL_STEP_NAMESPACE = UUID("73832f8b-ca66-47cb-bb41-e95579f43d61")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileRunCreation:
    run: PlanningRun
    created: bool


class ProfileAgentRunService:
    def __init__(
        self,
        *,
        repository: OrchestrationRepository,
        apply_service: ProfileDraftApplyService,
        clock: Clock,
        retry_policy: RetryPolicy,
        enabled: bool,
    ) -> None:
        self._repository = repository
        self._apply_service = apply_service
        self._clock = clock
        self._retry_policy = retry_policy
        self._enabled = enabled

    async def create_run(
        self,
        user: UserAccount,
        *,
        client_request_id: str,
        user_message: str,
        current_week: date,
    ) -> ProfileRunCreation:
        self._require_enabled()
        request_id = client_request_id.strip()
        message = user_message.strip()
        if not request_id or not message:
            raise RunIdempotencyConflict("Run input must not be blank.")
        if current_week.weekday() != 0:
            raise RunIdempotencyConflict("current_week must be a Monday.")
        payload: JsonObject = {
            "user_message": message,
            "current_week": current_week.isoformat(),
        }
        canonical = json.dumps(
            {"user_id": str(user.id), "input": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        run_id = uuid5(
            _PROFILE_RUN_ID_NAMESPACE,
            f"{user.id}:{request_id}",
        )
        now = self._clock.now()
        run = PlanningRun(
            id=run_id,
            user_id=user.id,
            workflow_type=WorkflowType.PROFILE_AGENT_REVIEW,
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
            id=uuid5(_PROFILE_INITIAL_STEP_NAMESPACE, f"{run.id}:1"),
            run_id=run.id,
            step_type=StepType.PARSE_PROFILE_REQUEST,
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
        return ProfileRunCreation(run=result.run, created=result.created)

    async def get_run(self, user: UserAccount, run_id: UUID) -> PlanningRun:
        run = await self._repository.get_run_for_user(run_id, user.id)
        if run is None or run.workflow_type is not WorkflowType.PROFILE_AGENT_REVIEW:
            raise RunNotFound("Profile Agent run was not found.")
        return run

    async def list_steps(self, user: UserAccount, run_id: UUID) -> list[AgentStep]:
        run = await self.get_run(user, run_id)
        return await self._repository.list_steps(run.id)

    async def list_checkpoints(
        self,
        user: UserAccount,
        run_id: UUID,
    ) -> list[StepCheckpoint]:
        run = await self.get_run(user, run_id)
        return await self._repository.list_checkpoints(run.id)

    async def list_audit(
        self,
        user: UserAccount,
        run_id: UUID,
    ) -> list[OrchestrationAuditEvent]:
        run = await self.get_run(user, run_id)
        return await self._repository.list_audit_events(run.id)

    async def submit_apply(
        self,
        user: UserAccount,
        run_id: UUID,
        decision: ProfileDraftApplyDecision,
    ) -> PlanningRun:
        self._require_enabled()
        run = await self.get_run(user, run_id)
        if run.status not in {
            PlanningRunStatus.WAITING_PROFILE_REVIEW,
            PlanningRunStatus.APPLYING_PROFILE_DRAFT,
            PlanningRunStatus.FINALIZING_PROFILE_RUN,
            PlanningRunStatus.COMPLETED,
        }:
            raise ProfileRunNotWaitingReview(
                "Profile Agent run is not waiting for review."
            )
        waiting = await self._review_step(run.id)
        draft_id = self._draft_id(waiting)
        payload: JsonObject = {
            "draft_id": str(draft_id),
            "decision": decision_to_payload(decision),
        }
        try:
            await self._repository.resume_waiting_step(
                run_id=run.id,
                expected_step_id=waiting.id,
                handler_version="phase-3b-user-apply-decision-v1",
                next_step_type=StepType.APPLY_PROFILE_DRAFT,
                now=self._clock.now(),
                resume_payload=payload,
            )
        except CheckpointConflict as exc:
            raise ProfileDraftApplyIdempotencyConflict(str(exc)) from exc
        return await self.get_run(user, run.id)

    async def reject_run(
        self,
        user: UserAccount,
        run_id: UUID,
        *,
        client_request_id: str,
        expected_draft_version: int,
    ) -> tuple[PlanningRun, ProfileDraftRejectResult]:
        self._require_enabled()
        run = await self.get_run(user, run_id)
        if run.status not in {
            PlanningRunStatus.WAITING_PROFILE_REVIEW,
            PlanningRunStatus.CANCELLED,
        }:
            raise ProfileRunNotWaitingReview(
                "Profile Agent run is not waiting for rejection."
            )
        waiting = await self._review_step(run.id, include_cancelled=True)
        result = await self._apply_service.reject(
            user=user,
            draft_id=self._draft_id(waiting),
            client_request_id=client_request_id,
            expected_draft_version=expected_draft_version,
        )
        try:
            cancelled = await self._repository.cancel_run(
                run_id=run.id,
                now=self._clock.now(),
            )
        except InvalidRunStateTransition as exc:
            raise RunAlreadyTerminal(str(exc)) from exc
        return cancelled, result

    async def _review_step(
        self,
        run_id: UUID,
        *,
        include_cancelled: bool = False,
    ) -> AgentStep:
        states = {AgentStepStatus.WAITING_USER, AgentStepStatus.SUCCEEDED}
        if include_cancelled:
            states.add(AgentStepStatus.CANCELLED)
        matches = [
            item
            for item in await self._repository.list_steps(run_id)
            if item.step_type is StepType.WAIT_FOR_PROFILE_DRAFT_REVIEW
            and item.status in states
        ]
        if len(matches) != 1:
            raise ProfileRunNotWaitingReview(
                "Profile Agent run has no single review checkpoint."
            )
        return matches[0]

    @staticmethod
    def _draft_id(step: AgentStep) -> UUID:
        try:
            payload = step.output_payload or step.input_payload
            return UUID(str(payload["draft_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileRunNotWaitingReview(
                "Review checkpoint has no Draft reference."
            ) from exc

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise OrchestratorDisabled("The in-memory orchestrator is disabled.")
