"""Atomic single-process orchestration repository backed only by memory."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4, uuid5

from app.domain.common import DomainValidationError
from app.domain.orchestration.enums import (
    AgentStepStatus,
    AuditEventType,
    PlanningRunStatus,
    StepType,
    WorkflowType,
)
from app.domain.orchestration.errors import (
    CheckpointConflict,
    InvalidRunStateTransition,
    MultipleWaitingSteps,
    RunIdempotencyConflict,
    StepLeaseLost,
    StepNotFound,
)
from app.domain.orchestration.models import (
    AgentStep,
    ClaimedStep,
    JsonObject,
    OrchestrationAuditEvent,
    PlanningRun,
    RunCreationResult,
    StepCheckpoint,
    detached,
    safe_json_object,
)
from app.domain.orchestration.state_machine import (
    RUN_TERMINAL_STATUSES,
    STEP_TERMINAL_STATUSES,
    transition_run,
    transition_step,
)
from app.orchestration.metrics import OrchestratorMetrics

_STEP_ID_NAMESPACE = UUID("52003553-d966-4a63-85c8-9c9f163ec4ae")
_CHECKPOINT_ID_NAMESPACE = UUID("fe36a55f-f075-45d2-85cf-59eebec8d022")

_CLAIM_RUN_STATUS = {
    StepType.LOAD_PROFILE_CONTEXT: PlanningRunStatus.COLLECTING_PROFILE,
    StepType.GENERATE_DETERMINISTIC_PLAN: PlanningRunStatus.GENERATING_SESSIONS,
    StepType.VERIFY_PLAN_SAFETY: PlanningRunStatus.SAFETY_VALIDATING,
    StepType.WAIT_FOR_USER_CONFIRMATION: PlanningRunStatus.SAFETY_VALIDATING,
    StepType.FINALIZE_RUN: PlanningRunStatus.WAITING_CONFIRMATION,
    StepType.PARSE_PROFILE_REQUEST: PlanningRunStatus.PARSING_PROFILE_REQUEST,
    StepType.WAIT_FOR_PROFILE_DRAFT_REVIEW: (PlanningRunStatus.PARSING_PROFILE_REQUEST),
    StepType.APPLY_PROFILE_DRAFT: PlanningRunStatus.APPLYING_PROFILE_DRAFT,
    StepType.FINALIZE_PROFILE_RUN: PlanningRunStatus.FINALIZING_PROFILE_RUN,
    StepType.LOAD_SESSION_APPLICATION_CONTEXT: (
        PlanningRunStatus.LOADING_SESSION_APPLICATION_CONTEXT
    ),
    StepType.VALIDATE_SESSION_DESIGN_TARGET: (
        PlanningRunStatus.VALIDATING_SESSION_DESIGN_TARGET
    ),
    StepType.BUILD_SESSION_PLAN_REVISION: (
        PlanningRunStatus.BUILDING_SESSION_PLAN_REVISION
    ),
    StepType.VERIFY_SESSION_PLAN_SAFETY: (
        PlanningRunStatus.VERIFYING_SESSION_PLAN_SAFETY
    ),
    StepType.WAIT_FOR_PLAN_REVISION_CONFIRMATION: (
        PlanningRunStatus.VERIFYING_SESSION_PLAN_SAFETY
    ),
    StepType.FINALIZE_SESSION_APPLICATION: (
        PlanningRunStatus.FINALIZING_SESSION_APPLICATION
    ),
    StepType.LOAD_SCHEDULE_APPLICATION_CONTEXT: (
        PlanningRunStatus.LOADING_SCHEDULE_APPLICATION_CONTEXT
    ),
    StepType.VALIDATE_SCHEDULE_APPLICATION: (
        PlanningRunStatus.VALIDATING_SCHEDULE_APPLICATION
    ),
    StepType.REVALIDATE_CALENDAR_BUSY: (PlanningRunStatus.REVALIDATING_CALENDAR_BUSY),
    StepType.BUILD_SCHEDULE_PLAN_REVISION: (
        PlanningRunStatus.BUILDING_SCHEDULE_PLAN_REVISION
    ),
    StepType.VERIFY_SCHEDULE_PLAN_SAFETY: (
        PlanningRunStatus.VERIFYING_SCHEDULE_PLAN_SAFETY
    ),
    StepType.WAIT_FOR_SCHEDULE_REVISION_CONFIRMATION: (
        PlanningRunStatus.VERIFYING_SCHEDULE_PLAN_SAFETY
    ),
    StepType.FINALIZE_SCHEDULE_APPLICATION: (
        PlanningRunStatus.FINALIZING_SCHEDULE_APPLICATION
    ),
    StepType.LOAD_CALENDAR_OPERATION_DRAFT: (
        PlanningRunStatus.LOADING_CALENDAR_OPERATION
    ),
    StepType.VALIDATE_CALENDAR_OPERATION_APPROVAL: (
        PlanningRunStatus.VALIDATING_CALENDAR_OPERATION
    ),
    StepType.EXECUTE_CALENDAR_OPERATION_ITEMS: (
        PlanningRunStatus.EXECUTING_CALENDAR_OPERATION
    ),
    StepType.VERIFY_CALENDAR_OPERATION_RESULTS: (
        PlanningRunStatus.VERIFYING_CALENDAR_OPERATION
    ),
    StepType.FINALIZE_CALENDAR_OPERATION: (
        PlanningRunStatus.FINALIZING_CALENDAR_OPERATION
    ),
    StepType.LOAD_RECOVERY_APPLICATION_CONTEXT: (
        PlanningRunStatus.LOADING_RECOVERY_APPLICATION_CONTEXT
    ),
    StepType.VALIDATE_RECOVERY_DRAFT: PlanningRunStatus.VALIDATING_RECOVERY_DRAFT,
    StepType.RESOLVE_RECOVERY_ACTIONS: PlanningRunStatus.RESOLVING_RECOVERY_ACTIONS,
    StepType.CREATE_RECOVERY_SUBDRAFTS: (PlanningRunStatus.CREATING_RECOVERY_SUBDRAFTS),
    StepType.WAIT_FOR_RECOVERY_SUBDRAFT_REVIEWS: (
        PlanningRunStatus.CREATING_RECOVERY_SUBDRAFTS
    ),
    StepType.BUILD_RECOVERY_PLAN_REVISION: (
        PlanningRunStatus.BUILDING_RECOVERY_PLAN_REVISION
    ),
    StepType.VERIFY_RECOVERY_PLAN_SAFETY: (
        PlanningRunStatus.VERIFYING_RECOVERY_PLAN_SAFETY
    ),
    StepType.WAIT_FOR_RECOVERY_REVISION_CONFIRMATION: (
        PlanningRunStatus.VERIFYING_RECOVERY_PLAN_SAFETY
    ),
    StepType.FINALIZE_RECOVERY_APPLICATION: (
        PlanningRunStatus.FINALIZING_RECOVERY_APPLICATION
    ),
}


class InMemoryOrchestrationRepository:
    """One-lock adapter providing atomic claim, lease, and checkpoint semantics."""

    def __init__(self, metrics: OrchestratorMetrics | None = None) -> None:
        self._lock = asyncio.Lock()
        self._runs: dict[UUID, PlanningRun] = {}
        self._steps: dict[UUID, AgentStep] = {}
        self._run_steps: dict[UUID, list[UUID]] = {}
        self._request_index: dict[tuple[UUID, WorkflowType, str], UUID] = {}
        self._checkpoints: dict[UUID, StepCheckpoint] = {}
        self._checkpoint_keys: dict[tuple[UUID, int], UUID] = {}
        self._audit: dict[UUID, list[OrchestrationAuditEvent]] = {}
        self._audit_sequence: dict[UUID, int] = {}
        self.metrics = metrics or OrchestratorMetrics()

    async def create_run_with_initial_steps(
        self,
        run: PlanningRun,
        steps: tuple[AgentStep, ...],
    ) -> RunCreationResult:
        async with self._lock:
            key = (run.user_id, run.workflow_type, run.client_request_id)
            existing_id = self._request_index.get(key)
            if existing_id is not None:
                existing = self._runs[existing_id]
                if existing.request_fingerprint != run.request_fingerprint:
                    raise RunIdempotencyConflict(
                        "The client request id belongs to a different payload."
                    )
                return RunCreationResult(run=detached(existing), created=False)
            if run.id in self._runs:
                raise RunIdempotencyConflict("Planning run id already exists.")
            if run.status is not PlanningRunStatus.CREATED:
                raise InvalidRunStateTransition("A new run must start in CREATED.")
            if not steps:
                raise DomainValidationError("At least one initial step is required.")
            if any(step.run_id != run.id for step in steps):
                raise DomainValidationError(
                    "Every initial step must belong to the run."
                )
            if len({step.id for step in steps}) != len(steps):
                raise DomainValidationError("Initial step ids must be unique.")
            if any(step.id in self._steps for step in steps):
                raise DomainValidationError("Initial step id already exists.")

            queued = transition_run(run, PlanningRunStatus.QUEUED)
            queued = replace(
                queued,
                current_step_id=steps[0].id,
                updated_at=run.created_at,
                version=run.version + 1,
            )
            self._runs[run.id] = detached(queued)
            self._run_steps[run.id] = []
            self._audit[run.id] = []
            self._audit_sequence[run.id] = 0
            self._request_index[key] = run.id
            self._append_audit_locked(
                run_id=run.id,
                event_type=AuditEventType.RUN_CREATED,
                now=run.created_at,
                from_status=None,
                to_status=PlanningRunStatus.CREATED.value,
            )
            if run.workflow_type is WorkflowType.SESSION_DESIGN_PLAN_INTEGRATION:
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=(AuditEventType.SESSION_DESIGN_APPLICATION_RUN_CREATED),
                    now=run.created_at,
                    metadata={
                        key: value
                        for key in (
                            "draft_id",
                            "root_plan_id",
                            "source_revision",
                            "target_session_id",
                        )
                        if isinstance((value := run.input_payload.get(key)), (str, int))
                    },
                )
            elif run.workflow_type is WorkflowType.SCHEDULE_PLAN_INTEGRATION:
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=AuditEventType.SCHEDULE_APPLICATION_RUN_CREATED,
                    now=run.created_at,
                    metadata=self._phase6b_metadata(run.input_payload),
                )
            elif run.workflow_type is WorkflowType.CALENDAR_OPERATION_EXECUTION:
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=AuditEventType.CALENDAR_OPERATION_RUN_CREATED,
                    now=run.created_at,
                    metadata=self._phase6b_metadata(run.input_payload),
                )
            elif run.workflow_type is WorkflowType.RECOVERY_PLAN_INTEGRATION:
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=AuditEventType.RECOVERY_APPLICATION_RUN_CREATED,
                    now=run.created_at,
                    metadata=self._recovery_metadata(run.input_payload),
                )
            self._append_audit_locked(
                run_id=run.id,
                event_type=AuditEventType.RUN_STATUS_CHANGED,
                now=run.created_at,
                from_status=PlanningRunStatus.CREATED.value,
                to_status=PlanningRunStatus.QUEUED.value,
            )
            for step in sorted(steps, key=self._step_sort_key):
                self._steps[step.id] = detached(step)
                self._run_steps[run.id].append(step.id)
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=step.id,
                    event_type=AuditEventType.STEP_CREATED,
                    now=run.created_at,
                    from_status=None,
                    to_status=step.status.value,
                    metadata={"step_type": step.step_type.value},
                )
            self.metrics.runs_created += 1
            return RunCreationResult(run=detached(queued), created=True)

    async def get_run(self, run_id: UUID) -> PlanningRun | None:
        async with self._lock:
            run = self._runs.get(run_id)
            return detached(run) if run is not None else None

    async def get_run_for_user(
        self,
        run_id: UUID,
        user_id: UUID,
    ) -> PlanningRun | None:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.user_id != user_id:
                return None
            return detached(run)

    async def list_steps(self, run_id: UUID) -> list[AgentStep]:
        async with self._lock:
            return [
                detached(self._steps[step_id])
                for step_id in sorted(
                    self._run_steps.get(run_id, ()),
                    key=lambda item: self._step_sort_key(self._steps[item]),
                )
            ]

    async def claim_next_step(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
        now: datetime,
    ) -> ClaimedStep | None:
        if not worker_id.strip() or lease_duration.total_seconds() <= 0:
            raise ValueError("worker_id and lease_duration must be valid.")
        async with self._lock:
            candidates = [
                step
                for step in self._steps.values()
                if step.status
                in {AgentStepStatus.READY, AgentStepStatus.RETRY_SCHEDULED}
                and step.next_execute_at <= now
                and self._dependencies_succeeded_locked(step)
                and self._runs[step.run_id].status not in RUN_TERMINAL_STATUSES
            ]
            if not candidates:
                return None
            current = min(candidates, key=self._step_sort_key)
            previous_step_status = current.status
            if current.status is AgentStepStatus.RETRY_SCHEDULED:
                current = transition_step(current, AgentStepStatus.READY)
            current = transition_step(current, AgentStepStatus.RUNNING)
            token = uuid4()
            current = replace(
                current,
                attempt_count=current.attempt_count + 1,
                fencing_token=current.fencing_token + 1,
                worker_id=worker_id,
                lease_token=token,
                lease_expires_at=now + lease_duration,
                heartbeat_at=now,
                updated_at=now,
                version=current.version + 1,
            )
            self._steps[current.id] = current

            run = self._runs[current.run_id]
            target_run_status = _CLAIM_RUN_STATUS[current.step_type]
            if run.status is not target_run_status:
                previous_run_status = run.status
                run = transition_run(run, target_run_status)
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=AuditEventType.RUN_STATUS_CHANGED,
                    now=now,
                    from_status=previous_run_status.value,
                    to_status=target_run_status.value,
                )
            run = replace(
                run,
                current_step_id=current.id,
                updated_at=now,
                version=run.version + 1,
            )
            self._runs[run.id] = run
            self._append_audit_locked(
                run_id=run.id,
                step_id=current.id,
                event_type=AuditEventType.STEP_CLAIMED,
                now=now,
                from_status=previous_step_status.value,
                to_status=AgentStepStatus.RUNNING.value,
                worker_id=worker_id,
                attempt_no=current.attempt_count,
            )
            self.metrics.steps_claimed += 1
            self.metrics.step_attempts += 1
            return ClaimedStep(run=detached(run), step=detached(current))

    async def heartbeat(
        self,
        *,
        step_id: UUID,
        worker_id: str,
        lease_token: UUID,
        fencing_token: int,
        lease_duration: timedelta,
        now: datetime,
    ) -> AgentStep:
        async with self._lock:
            current = self._validate_lease_locked(
                step_id=step_id,
                worker_id=worker_id,
                lease_token=lease_token,
                now=now,
            )
            if current.fencing_token != fencing_token:
                self.metrics.lease_conflicts += 1
                raise StepLeaseLost(
                    "Worker no longer owns the active fencing generation."
                )
            updated = replace(
                current,
                heartbeat_at=now,
                lease_expires_at=now + lease_duration,
                updated_at=now,
                version=current.version + 1,
            )
            self._steps[step_id] = updated
            self._append_audit_locked(
                run_id=updated.run_id,
                step_id=updated.id,
                event_type=AuditEventType.STEP_HEARTBEAT,
                now=now,
                from_status=updated.status.value,
                to_status=updated.status.value,
                worker_id=worker_id,
                attempt_no=updated.attempt_count,
            )
            return detached(updated)

    async def complete_step(
        self,
        *,
        claim: ClaimedStep,
        handler_version: str,
        output_payload: JsonObject,
        result_reference: str | None,
        next_step_type: StepType | None,
        run_status_after: PlanningRunStatus,
        now: datetime,
    ) -> AgentStep:
        payload = safe_json_object(output_payload)
        self._validate_reference(result_reference)
        async with self._lock:
            current = self._validate_claim_locked(claim, now)
            completed = transition_step(current, AgentStepStatus.SUCCEEDED)
            completed = replace(
                completed,
                output_payload=payload,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
                completed_at=now,
                updated_at=now,
                version=current.version + 1,
            )
            self._steps[completed.id] = completed

            run = self._runs[completed.run_id]
            previous_run_status = run.status
            if run.status is not run_status_after:
                run = transition_run(run, run_status_after)
            next_step = self._create_next_step_locked(
                current=completed,
                next_step_type=next_step_type,
                payload=payload,
                now=now,
            )
            run = replace(
                run,
                result_reference=result_reference or run.result_reference,
                current_step_id=next_step.id if next_step is not None else None,
                updated_at=now,
                completed_at=(
                    now if run_status_after is PlanningRunStatus.COMPLETED else None
                ),
                version=run.version + 1,
            )
            self._runs[run.id] = run
            checkpoint = self._build_checkpoint(
                step=completed,
                handler_version=handler_version,
                output_payload=payload,
                result_reference=result_reference,
                run_status_after=run.status,
                now=now,
            )
            self._save_checkpoint_locked(checkpoint)
            self._append_audit_locked(
                run_id=run.id,
                step_id=completed.id,
                event_type=AuditEventType.STEP_SUCCEEDED,
                now=now,
                from_status=AgentStepStatus.RUNNING.value,
                to_status=AgentStepStatus.SUCCEEDED.value,
                worker_id=claim.step.worker_id,
                attempt_no=completed.attempt_count,
            )
            if previous_run_status is not run.status:
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=AuditEventType.RUN_STATUS_CHANGED,
                    now=now,
                    from_status=previous_run_status.value,
                    to_status=run.status.value,
                )
            if next_step is not None:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=next_step.id,
                    event_type=AuditEventType.STEP_CREATED,
                    now=now,
                    from_status=None,
                    to_status=next_step.status.value,
                    metadata={"step_type": next_step.step_type.value},
                )
            if completed.step_type is StepType.PARSE_PROFILE_REQUEST:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.PROFILE_DRAFT_CREATED,
                    now=now,
                    metadata={
                        key: value
                        for key in ("draft_id", "request_id")
                        if isinstance((value := payload.get(key)), str)
                    },
                )
            elif completed.step_type is StepType.APPLY_PROFILE_DRAFT:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.PROFILE_DRAFT_APPLIED,
                    now=now,
                    metadata={
                        key: value
                        for key in (
                            "draft_id",
                            "apply_result_id",
                            "resulting_profile_version",
                        )
                        if isinstance(
                            (value := payload.get(key)),
                            (str, int),
                        )
                    },
                )
            elif completed.step_type is StepType.VALIDATE_SESSION_DESIGN_TARGET:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.SESSION_DESIGN_TARGET_VALIDATED,
                    now=now,
                    metadata=self._session_application_metadata(payload),
                )
            elif completed.step_type is StepType.BUILD_SESSION_PLAN_REVISION:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.SESSION_DESIGN_PLAN_REVISION_CREATED,
                    now=now,
                    metadata=self._session_application_metadata(payload),
                )
            elif completed.step_type is StepType.VERIFY_SESSION_PLAN_SAFETY:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.SESSION_DESIGN_PLAN_SAFETY_PASSED,
                    now=now,
                    metadata=self._session_application_metadata(payload),
                )
            elif completed.step_type is StepType.BUILD_SCHEDULE_PLAN_REVISION:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.SCHEDULE_PLAN_REVISION_CREATED,
                    now=now,
                    metadata=self._phase6b_metadata(payload),
                )
            elif completed.step_type is StepType.EXECUTE_CALENDAR_OPERATION_ITEMS:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.CALENDAR_OPERATION_EXECUTED,
                    now=now,
                    metadata=self._phase6b_metadata(payload),
                )
            elif completed.step_type is StepType.VALIDATE_RECOVERY_DRAFT:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.RECOVERY_DRAFT_VALIDATED,
                    now=now,
                    metadata=self._recovery_metadata(payload),
                )
            elif completed.step_type is StepType.RESOLVE_RECOVERY_ACTIONS:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.RECOVERY_ACTIONS_RESOLVED,
                    now=now,
                    metadata=self._recovery_metadata(payload),
                )
            elif completed.step_type is StepType.CREATE_RECOVERY_SUBDRAFTS:
                for event_type, key in (
                    (
                        AuditEventType.RECOVERY_SESSION_DESIGN_DRAFT_CREATED,
                        "session_design_draft_ids",
                    ),
                    (
                        AuditEventType.RECOVERY_SCHEDULE_DRAFT_CREATED,
                        "schedule_draft_ids",
                    ),
                ):
                    values = payload.get(key)
                    if isinstance(values, list) and values:
                        self._append_audit_locked(
                            run_id=run.id,
                            step_id=completed.id,
                            event_type=event_type,
                            now=now,
                            metadata={key: values},
                        )
            elif completed.step_type is StepType.BUILD_RECOVERY_PLAN_REVISION:
                if payload.get("created_revision") is not None:
                    self._append_audit_locked(
                        run_id=run.id,
                        step_id=completed.id,
                        event_type=AuditEventType.RECOVERY_PLAN_REVISION_CREATED,
                        now=now,
                        metadata=self._recovery_metadata(payload),
                    )
            elif completed.step_type is StepType.VERIFY_RECOVERY_PLAN_SAFETY:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.RECOVERY_PLAN_SAFETY_PASSED,
                    now=now,
                    metadata=self._recovery_metadata(payload),
                )
            if run.status is PlanningRunStatus.COMPLETED:
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=AuditEventType.RUN_COMPLETED,
                    now=now,
                    from_status=previous_run_status.value,
                    to_status=run.status.value,
                )
                self.metrics.runs_completed += 1
                if run.workflow_type is WorkflowType.PROFILE_AGENT_REVIEW:
                    self._append_audit_locked(
                        run_id=run.id,
                        step_id=completed.id,
                        event_type=AuditEventType.PROFILE_RUN_COMPLETED,
                        now=now,
                        metadata=(
                            {"apply_result_id": apply_result_id}
                            if isinstance(
                                (apply_result_id := payload.get("apply_result_id")),
                                str,
                            )
                            else {}
                        ),
                    )
                elif run.workflow_type is WorkflowType.SESSION_DESIGN_PLAN_INTEGRATION:
                    self._append_audit_locked(
                        run_id=run.id,
                        step_id=completed.id,
                        event_type=(
                            AuditEventType.SESSION_DESIGN_APPLICATION_COMPLETED
                        ),
                        now=now,
                        metadata=self._session_application_metadata(payload),
                    )
                elif run.workflow_type is WorkflowType.SCHEDULE_PLAN_INTEGRATION:
                    self._append_audit_locked(
                        run_id=run.id,
                        step_id=completed.id,
                        event_type=AuditEventType.SCHEDULE_APPLICATION_COMPLETED,
                        now=now,
                        metadata=self._phase6b_metadata(payload),
                    )
                elif run.workflow_type is WorkflowType.CALENDAR_OPERATION_EXECUTION:
                    self._append_audit_locked(
                        run_id=run.id,
                        step_id=completed.id,
                        event_type=AuditEventType.CALENDAR_OPERATION_COMPLETED,
                        now=now,
                        metadata=self._phase6b_metadata(payload),
                    )
                elif run.workflow_type is WorkflowType.RECOVERY_PLAN_INTEGRATION:
                    self._append_audit_locked(
                        run_id=run.id,
                        step_id=completed.id,
                        event_type=AuditEventType.RECOVERY_APPLICATION_COMPLETED,
                        now=now,
                        metadata=self._recovery_metadata(payload),
                    )
            self.metrics.steps_succeeded += 1
            return detached(completed)

    async def fail_step(
        self,
        *,
        claim: ClaimedStep,
        error_code: str,
        error_message: str,
        retry_at: datetime | None,
        now: datetime,
    ) -> AgentStep:
        safe_message = self._safe_error_message(error_message)
        async with self._lock:
            current = self._validate_claim_locked(claim, now)
            run = self._runs[current.run_id]
            previous_run_status = run.status
            retryable = (
                retry_at is not None and current.attempt_count < current.max_attempts
            )
            if retryable:
                assert retry_at is not None
                failed = transition_step(current, AgentStepStatus.FAILED_RETRYABLE)
                failed = transition_step(failed, AgentStepStatus.RETRY_SCHEDULED)
                failed = replace(
                    failed,
                    next_execute_at=retry_at,
                    worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    last_error_code=error_code[:80],
                    last_error_message=safe_message,
                    updated_at=now,
                    version=current.version + 1,
                )
                if run.status is not PlanningRunStatus.FAILED_RETRYABLE:
                    run = transition_run(run, PlanningRunStatus.FAILED_RETRYABLE)
                event_type = AuditEventType.STEP_RETRY_SCHEDULED
                self.metrics.retries_scheduled += 1
            else:
                failed = transition_step(current, AgentStepStatus.FAILED_PERMANENT)
                failed = replace(
                    failed,
                    worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    last_error_code=error_code[:80],
                    last_error_message=safe_message,
                    completed_at=now,
                    updated_at=now,
                    version=current.version + 1,
                )
                run = transition_run(run, PlanningRunStatus.FAILED_PERMANENT)
                event_type = AuditEventType.STEP_FAILED_PERMANENT
                self.metrics.runs_failed += 1
            self._steps[failed.id] = failed
            run = replace(
                run, current_step_id=failed.id, updated_at=now, version=run.version + 1
            )
            self._runs[run.id] = run
            self._append_audit_locked(
                run_id=run.id,
                step_id=failed.id,
                event_type=event_type,
                now=now,
                from_status=AgentStepStatus.RUNNING.value,
                to_status=failed.status.value,
                worker_id=claim.step.worker_id,
                attempt_no=failed.attempt_count,
                error_code=error_code[:80],
                metadata={"error_message": safe_message},
            )
            if previous_run_status is not run.status:
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=AuditEventType.RUN_STATUS_CHANGED,
                    now=now,
                    from_status=previous_run_status.value,
                    to_status=run.status.value,
                )
            return detached(failed)

    async def mark_waiting_user(
        self,
        *,
        claim: ClaimedStep,
        output_payload: JsonObject,
        run_status_after: PlanningRunStatus,
        now: datetime,
    ) -> AgentStep:
        payload = safe_json_object(output_payload)
        async with self._lock:
            current = self._validate_claim_locked(claim, now)
            waiting = transition_step(current, AgentStepStatus.WAITING_USER)
            waiting = replace(
                waiting,
                output_payload=payload,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
                updated_at=now,
                version=current.version + 1,
            )
            self._steps[waiting.id] = waiting
            run = self._runs[waiting.run_id]
            previous_run_status = run.status
            if run.status is not run_status_after:
                run = transition_run(run, run_status_after)
            run = replace(
                run,
                current_step_id=waiting.id,
                updated_at=now,
                version=run.version + 1,
            )
            self._runs[run.id] = run
            self._append_audit_locked(
                run_id=run.id,
                step_id=waiting.id,
                event_type=AuditEventType.STEP_WAITING_USER,
                now=now,
                from_status=AgentStepStatus.RUNNING.value,
                to_status=AgentStepStatus.WAITING_USER.value,
                attempt_no=waiting.attempt_count,
            )
            if waiting.step_type is StepType.WAIT_FOR_PROFILE_DRAFT_REVIEW:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=waiting.id,
                    event_type=AuditEventType.PROFILE_DRAFT_WAITING_REVIEW,
                    now=now,
                    metadata=(
                        {"draft_id": draft_id}
                        if isinstance((draft_id := payload.get("draft_id")), str)
                        else {}
                    ),
                )
            elif waiting.step_type is StepType.WAIT_FOR_PLAN_REVISION_CONFIRMATION:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=waiting.id,
                    event_type=(
                        AuditEventType.SESSION_DESIGN_PLAN_WAITING_CONFIRMATION
                    ),
                    now=now,
                    metadata=self._session_application_metadata(payload),
                )
            elif waiting.step_type is StepType.WAIT_FOR_SCHEDULE_REVISION_CONFIRMATION:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=waiting.id,
                    event_type=AuditEventType.SCHEDULE_PLAN_WAITING_CONFIRMATION,
                    now=now,
                    metadata=self._phase6b_metadata(payload),
                )
            elif waiting.step_type is StepType.WAIT_FOR_RECOVERY_SUBDRAFT_REVIEWS:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=waiting.id,
                    event_type=AuditEventType.RECOVERY_WAITING_SUBDRAFT_REVIEW,
                    now=now,
                    metadata=self._recovery_metadata(payload),
                )
            elif waiting.step_type is StepType.WAIT_FOR_RECOVERY_REVISION_CONFIRMATION:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=waiting.id,
                    event_type=AuditEventType.RECOVERY_PLAN_WAITING_CONFIRMATION,
                    now=now,
                    metadata=self._recovery_metadata(payload),
                )
            if previous_run_status is not run.status:
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=AuditEventType.RUN_STATUS_CHANGED,
                    now=now,
                    from_status=previous_run_status.value,
                    to_status=run.status.value,
                )
            self.metrics.waiting_user_count += 1
            return detached(waiting)

    async def resume_waiting_step(
        self,
        *,
        run_id: UUID,
        expected_step_id: UUID,
        handler_version: str,
        next_step_type: StepType,
        now: datetime,
        resume_payload: JsonObject | None = None,
    ) -> AgentStep:
        provided_payload = (
            safe_json_object(resume_payload) if resume_payload is not None else None
        )
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise StepNotFound("Run was not found.")
            expected = self._steps.get(expected_step_id)
            if expected is None or expected.run_id != run_id:
                raise StepNotFound("Waiting step was not found.")
            if expected.status is AgentStepStatus.SUCCEEDED:
                if (
                    provided_payload is not None
                    and expected.output_payload != provided_payload
                ):
                    raise CheckpointConflict(
                        "The waiting step was resumed with another payload."
                    )
                return detached(expected)
            waiting_steps = [
                self._steps[step_id]
                for step_id in self._run_steps[run_id]
                if self._steps[step_id].status is AgentStepStatus.WAITING_USER
            ]
            if len(waiting_steps) != 1 or waiting_steps[0].id != expected_step_id:
                raise MultipleWaitingSteps(
                    "Run must contain exactly one expected waiting-user step."
                )
            payload = (
                provided_payload or expected.output_payload or expected.input_payload
            )
            completed = transition_step(expected, AgentStepStatus.SUCCEEDED)
            completed = replace(
                completed,
                output_payload=payload,
                completed_at=now,
                updated_at=now,
                version=expected.version + 1,
            )
            self._steps[completed.id] = completed
            next_step = self._create_next_step_locked(
                current=completed,
                next_step_type=next_step_type,
                payload=payload,
                now=now,
            )
            if next_step is None:
                raise DomainValidationError("Resume requires a finalization step.")
            run = replace(
                run,
                current_step_id=next_step.id,
                updated_at=now,
                version=run.version + 1,
            )
            self._runs[run.id] = run
            checkpoint = self._build_checkpoint(
                step=completed,
                handler_version=handler_version,
                output_payload=payload,
                result_reference=run.result_reference,
                run_status_after=run.status,
                now=now,
            )
            self._save_checkpoint_locked(checkpoint)
            self._append_audit_locked(
                run_id=run.id,
                step_id=completed.id,
                event_type=AuditEventType.STEP_RESUMED,
                now=now,
                from_status=AgentStepStatus.WAITING_USER.value,
                to_status=AgentStepStatus.SUCCEEDED.value,
                attempt_no=completed.attempt_count,
            )
            self._append_audit_locked(
                run_id=run.id,
                step_id=next_step.id,
                event_type=AuditEventType.STEP_CREATED,
                now=now,
                from_status=None,
                to_status=next_step.status.value,
                metadata={"step_type": next_step.step_type.value},
            )
            if completed.step_type is StepType.WAIT_FOR_PROFILE_DRAFT_REVIEW:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.PROFILE_DRAFT_APPLY_SUBMITTED,
                    now=now,
                    metadata={
                        **(
                            {"draft_id": draft_id}
                            if isinstance(
                                (draft_id := payload.get("draft_id")),
                                str,
                            )
                            else {}
                        ),
                        "apply_policy_version": "profile-draft-apply-v1",
                    },
                )
            elif completed.step_type is StepType.WAIT_FOR_PLAN_REVISION_CONFIRMATION:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=(AuditEventType.SESSION_DESIGN_PLAN_REVISION_CONFIRMED),
                    now=now,
                    metadata=self._session_application_metadata(payload),
                )
            elif completed.step_type is StepType.WAIT_FOR_RECOVERY_SUBDRAFT_REVIEWS:
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.RECOVERY_SUBDRAFTS_ACCEPTED,
                    now=now,
                    metadata=self._recovery_metadata(payload),
                )
            elif (
                completed.step_type is StepType.WAIT_FOR_RECOVERY_REVISION_CONFIRMATION
            ):
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.RECOVERY_PLAN_REVISION_CONFIRMED,
                    now=now,
                    metadata=self._recovery_metadata(payload),
                )
            self.metrics.steps_succeeded += 1
            self.metrics.waiting_user_count = max(
                0, self.metrics.waiting_user_count - 1
            )
            return detached(completed)

    async def reap_expired_steps(
        self,
        *,
        now: datetime,
        delays_seconds: tuple[int, ...],
    ) -> list[AgentStep]:
        if not delays_seconds:
            raise ValueError("delays_seconds must not be empty.")
        async with self._lock:
            expired = sorted(
                (
                    step
                    for step in self._steps.values()
                    if step.status is AgentStepStatus.RUNNING
                    and step.lease_expires_at is not None
                    and step.lease_expires_at < now
                ),
                key=self._step_sort_key,
            )
            reaped: list[AgentStep] = []
            for current in expired:
                self.metrics.leases_expired += 1
                self._append_audit_locked(
                    run_id=current.run_id,
                    step_id=current.id,
                    event_type=AuditEventType.STEP_LEASE_EXPIRED,
                    now=now,
                    from_status=current.status.value,
                    to_status=current.status.value,
                    worker_id=current.worker_id,
                    attempt_no=current.attempt_count,
                )
                run = self._runs[current.run_id]
                previous_run_status = run.status
                if current.attempt_count >= current.max_attempts:
                    updated = transition_step(current, AgentStepStatus.FAILED_PERMANENT)
                    updated = replace(
                        updated,
                        last_error_code="MAX_ATTEMPTS_EXCEEDED",
                        last_error_message=(
                            "Lease expired at the maximum attempt count."
                        ),
                        completed_at=now,
                    )
                    run = transition_run(run, PlanningRunStatus.FAILED_PERMANENT)
                    self.metrics.runs_failed += 1
                else:
                    updated = transition_step(current, AgentStepStatus.FAILED_RETRYABLE)
                    updated = transition_step(updated, AgentStepStatus.RETRY_SCHEDULED)
                    index = min(
                        max(current.attempt_count - 1, 0), len(delays_seconds) - 1
                    )
                    updated = replace(
                        updated,
                        next_execute_at=now + timedelta(seconds=delays_seconds[index]),
                        last_error_code="STEP_LEASE_EXPIRED",
                        last_error_message="Worker lease expired before completion.",
                    )
                    if run.status is not PlanningRunStatus.FAILED_RETRYABLE:
                        run = transition_run(run, PlanningRunStatus.FAILED_RETRYABLE)
                    self.metrics.retries_scheduled += 1
                updated = replace(
                    updated,
                    worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    updated_at=now,
                    version=current.version + 1,
                )
                self._steps[updated.id] = updated
                run = replace(
                    run,
                    current_step_id=updated.id,
                    updated_at=now,
                    version=run.version + 1,
                )
                self._runs[run.id] = run
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=updated.id,
                    event_type=AuditEventType.STEP_REAPED,
                    now=now,
                    from_status=AgentStepStatus.RUNNING.value,
                    to_status=updated.status.value,
                    attempt_no=updated.attempt_count,
                )
                if previous_run_status is not run.status:
                    self._append_audit_locked(
                        run_id=run.id,
                        event_type=AuditEventType.RUN_STATUS_CHANGED,
                        now=now,
                        from_status=previous_run_status.value,
                        to_status=run.status.value,
                    )
                self.metrics.steps_reaped += 1
                reaped.append(detached(updated))
            return reaped

    async def cancel_run(self, *, run_id: UUID, now: datetime) -> PlanningRun:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise InvalidRunStateTransition("Run was not found.")
            if run.status is PlanningRunStatus.CANCELLED:
                return detached(run)
            if run.status in RUN_TERMINAL_STATUSES:
                raise InvalidRunStateTransition("A terminal run cannot be cancelled.")
            previous = run.status
            run = transition_run(run, PlanningRunStatus.CANCELLED)
            run = replace(
                run,
                current_step_id=None,
                completed_at=now,
                updated_at=now,
                version=run.version + 1,
            )
            self._runs[run.id] = run
            for step_id in self._run_steps[run.id]:
                step = self._steps[step_id]
                if step.status in STEP_TERMINAL_STATUSES:
                    continue
                if step.status is AgentStepStatus.WAITING_USER:
                    self.metrics.waiting_user_count = max(
                        0,
                        self.metrics.waiting_user_count - 1,
                    )
                cancelled = transition_step(step, AgentStepStatus.CANCELLED)
                cancelled = replace(
                    cancelled,
                    worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    completed_at=now,
                    updated_at=now,
                    version=step.version + 1,
                )
                self._steps[step_id] = cancelled
                self._append_audit_locked(
                    run_id=run.id,
                    step_id=step_id,
                    event_type=AuditEventType.STEP_CANCELLED,
                    now=now,
                    from_status=step.status.value,
                    to_status=cancelled.status.value,
                )
            self._append_audit_locked(
                run_id=run.id,
                event_type=AuditEventType.RUN_STATUS_CHANGED,
                now=now,
                from_status=previous.value,
                to_status=run.status.value,
            )
            self._append_audit_locked(
                run_id=run.id,
                event_type=AuditEventType.RUN_CANCELLED,
                now=now,
                from_status=previous.value,
                to_status=run.status.value,
            )
            if run.workflow_type is WorkflowType.PROFILE_AGENT_REVIEW:
                draft_id = next(
                    (
                        step.output_payload.get("draft_id")
                        for step_id in self._run_steps[run.id]
                        if (step := self._steps[step_id]).output_payload is not None
                        and isinstance(step.output_payload.get("draft_id"), str)
                    ),
                    None,
                )
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=AuditEventType.PROFILE_DRAFT_REJECTED,
                    now=now,
                    from_status=previous.value,
                    to_status=run.status.value,
                    metadata=(
                        {"draft_id": draft_id} if isinstance(draft_id, str) else {}
                    ),
                )
            elif run.workflow_type is WorkflowType.SESSION_DESIGN_PLAN_INTEGRATION:
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=AuditEventType.SESSION_DESIGN_APPLICATION_CANCELLED,
                    now=now,
                    from_status=previous.value,
                    to_status=run.status.value,
                )
            elif run.workflow_type is WorkflowType.RECOVERY_PLAN_INTEGRATION:
                self._append_audit_locked(
                    run_id=run.id,
                    event_type=AuditEventType.RECOVERY_APPLICATION_CANCELLED,
                    now=now,
                    from_status=previous.value,
                    to_status=run.status.value,
                )
            return detached(run)

    async def list_checkpoints(self, run_id: UUID) -> list[StepCheckpoint]:
        async with self._lock:
            return [
                detached(item)
                for item in sorted(
                    (
                        checkpoint
                        for checkpoint in self._checkpoints.values()
                        if checkpoint.run_id == run_id
                    ),
                    key=lambda item: (
                        item.created_at,
                        str(item.step_id),
                        item.attempt_no,
                    ),
                )
            ]

    async def list_audit_events(
        self,
        run_id: UUID,
    ) -> list[OrchestrationAuditEvent]:
        async with self._lock:
            return [detached(item) for item in self._audit.get(run_id, ())]

    async def append_audit_event(
        self,
        event: OrchestrationAuditEvent,
    ) -> OrchestrationAuditEvent:
        async with self._lock:
            expected = self._audit_sequence.get(event.run_id, 0) + 1
            if event.sequence_no != expected:
                raise ValueError("Audit sequence must be contiguous per run.")
            self._audit.setdefault(event.run_id, []).append(detached(event))
            self._audit_sequence[event.run_id] = event.sequence_no
            return detached(event)

    async def save_checkpoint(self, checkpoint: StepCheckpoint) -> StepCheckpoint:
        async with self._lock:
            self._save_checkpoint_locked(checkpoint)
            return detached(checkpoint)

    async def reset(self) -> None:
        async with self._lock:
            self._runs.clear()
            self._steps.clear()
            self._run_steps.clear()
            self._request_index.clear()
            self._checkpoints.clear()
            self._checkpoint_keys.clear()
            self._audit.clear()
            self._audit_sequence.clear()
            self.metrics.reset()

    def _dependencies_succeeded_locked(self, step: AgentStep) -> bool:
        return all(
            dependency in self._steps
            and self._steps[dependency].status is AgentStepStatus.SUCCEEDED
            for dependency in step.dependency_step_ids
        )

    def _validate_claim_locked(
        self,
        claim: ClaimedStep,
        now: datetime,
    ) -> AgentStep:
        worker_id = claim.step.worker_id
        token = claim.step.lease_token
        if worker_id is None or token is None:
            self.metrics.lease_conflicts += 1
            raise StepLeaseLost("Claim has no active lease ownership.")
        return self._validate_lease_locked(
            step_id=claim.step.id,
            worker_id=worker_id,
            lease_token=token,
            now=now,
        )

    def _validate_lease_locked(
        self,
        *,
        step_id: UUID,
        worker_id: str,
        lease_token: UUID,
        now: datetime,
    ) -> AgentStep:
        current = self._steps.get(step_id)
        if current is None:
            raise StepNotFound("Step was not found.")
        if (
            current.status is not AgentStepStatus.RUNNING
            or current.worker_id != worker_id
            or current.lease_token != lease_token
            or current.lease_expires_at is None
            or current.lease_expires_at < now
        ):
            self.metrics.lease_conflicts += 1
            raise StepLeaseLost("Worker no longer owns the active step lease.")
        return current

    def _create_next_step_locked(
        self,
        *,
        current: AgentStep,
        next_step_type: StepType | None,
        payload: JsonObject,
        now: datetime,
    ) -> AgentStep | None:
        if next_step_type is None:
            return None
        sequence_no = current.sequence_no + 1
        step_id = uuid5(
            _STEP_ID_NAMESPACE,
            f"{current.run_id}:{sequence_no}:{next_step_type.value}",
        )
        existing = self._steps.get(step_id)
        if existing is not None:
            return existing
        next_step = AgentStep(
            id=step_id,
            run_id=current.run_id,
            step_type=next_step_type,
            status=AgentStepStatus.READY,
            sequence_no=sequence_no,
            priority=current.priority,
            input_payload=payload,
            output_payload=None,
            dependency_step_ids=(current.id,),
            attempt_count=0,
            max_attempts=current.max_attempts,
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
        self._steps[step_id] = next_step
        self._run_steps[current.run_id].append(step_id)
        return next_step

    def _build_checkpoint(
        self,
        *,
        step: AgentStep,
        handler_version: str,
        output_payload: JsonObject,
        result_reference: str | None,
        run_status_after: PlanningRunStatus,
        now: datetime,
    ) -> StepCheckpoint:
        canonical = json.dumps(
            step.input_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        checkpoint_id = uuid5(
            _CHECKPOINT_ID_NAMESPACE,
            f"{step.id}:{step.attempt_count}",
        )
        return StepCheckpoint(
            id=checkpoint_id,
            run_id=step.run_id,
            step_id=step.id,
            attempt_no=step.attempt_count,
            step_type=step.step_type,
            handler_version=handler_version,
            input_fingerprint=fingerprint,
            output_payload=output_payload,
            result_reference=result_reference,
            run_status_after=run_status_after,
            step_status_after=AgentStepStatus.SUCCEEDED,
            created_at=now,
        )

    def _save_checkpoint_locked(self, checkpoint: StepCheckpoint) -> None:
        key = (checkpoint.step_id, checkpoint.attempt_no)
        if key in self._checkpoint_keys or checkpoint.id in self._checkpoints:
            raise CheckpointConflict(
                "A checkpoint already exists for this successful attempt."
            )
        self._checkpoints[checkpoint.id] = detached(checkpoint)
        self._checkpoint_keys[key] = checkpoint.id

    def _append_audit_locked(
        self,
        *,
        run_id: UUID,
        event_type: AuditEventType,
        now: datetime,
        step_id: UUID | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        worker_id: str | None = None,
        attempt_no: int | None = None,
        error_code: str | None = None,
        metadata: JsonObject | None = None,
    ) -> None:
        sequence = self._audit_sequence.get(run_id, 0) + 1
        event = OrchestrationAuditEvent(
            id=uuid4(),
            sequence_no=sequence,
            run_id=run_id,
            step_id=step_id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            worker_id=worker_id,
            attempt_no=attempt_no,
            error_code=error_code,
            metadata=metadata or {},
            occurred_at=now,
        )
        self._audit.setdefault(run_id, []).append(event)
        self._audit_sequence[run_id] = sequence

    @staticmethod
    def _session_application_metadata(payload: JsonObject) -> JsonObject:
        allowed = (
            "draft_id",
            "draft_version",
            "application_result_id",
            "root_plan_id",
            "source_revision",
            "created_revision",
            "target_session_id",
            "context_snapshot_reference_id",
            "candidate_set_id",
            "application_fingerprint",
            "safety_passed",
            "policy_version",
        )
        return {
            key: value
            for key in allowed
            if isinstance((value := payload.get(key)), (str, int, bool))
        }

    @staticmethod
    def _phase6b_metadata(payload: JsonObject) -> JsonObject:
        allowed = (
            "draft_id",
            "application_result_id",
            "root_plan_id",
            "source_revision",
            "created_revision",
            "created_plan_revision_id",
            "resulting_plan_version",
            "calendar_verification_status",
            "draft_status",
        )
        return {
            key: value
            for key in allowed
            if isinstance((value := payload.get(key)), (str, int, bool))
        }

    @staticmethod
    def _recovery_metadata(payload: JsonObject) -> JsonObject:
        allowed = (
            "recovery_draft_id",
            "application_result_id",
            "root_plan_id",
            "source_revision",
            "created_revision",
            "resulting_plan_version",
            "application_fingerprint",
            "application_outcome",
            "safety_passed",
        )
        return {
            key: value
            for key in allowed
            if isinstance((value := payload.get(key)), (str, int, bool))
        }

    @staticmethod
    def _step_sort_key(step: AgentStep) -> tuple[int, datetime, int, datetime, str]:
        return (
            -step.priority,
            step.next_execute_at,
            step.sequence_no,
            step.created_at,
            str(step.id),
        )

    @staticmethod
    def _safe_error_message(message: str) -> str:
        normalized = " ".join(str(message).split())[:240]
        lowered = normalized.casefold()
        if "://" in normalized or any(
            item in lowered for item in ("password=", "database_url", "redis_url")
        ):
            return "Handler failed; sensitive details were redacted."
        return normalized or "Handler failed without a client-safe message."

    @staticmethod
    def _validate_reference(reference: str | None) -> None:
        if reference is not None and (not reference.strip() or "://" in reference):
            raise DomainValidationError("result_reference must be a safe opaque value.")
