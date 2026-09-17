"""MySQL-backed durable orchestration repository."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import delete, exists, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

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
    safe_json_object,
)
from app.domain.orchestration.state_machine import (
    RUN_TERMINAL_STATUSES,
    STEP_TERMINAL_STATUSES,
    transition_run,
    transition_step,
)
from app.persistence.memory.orchestration_repository import _CLAIM_RUN_STATUS
from app.persistence.mysql.models import (
    AgentStepModel,
    AuditEventModel,
    CheckpointModel,
    PlanningRunModel,
    StepDependencyModel,
)

_STEP_ID_NAMESPACE = UUID("52003553-d966-4a63-85c8-9c9f163ec4ae")
_CHECKPOINT_ID_NAMESPACE = UUID("fe36a55f-f075-45d2-85cf-59eebec8d022")
_CLAIM_AVAILABILITY_ATTEMPTS = 5
_CLAIM_LOCK_ERROR_ATTEMPTS = 2


class MySQLOrchestrationRepository:
    """MySQL fact-source adapter with one bounded transaction per transition."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        execution_user_id: UUID | None = None,
    ) -> None:
        self.session_factory = session_factory
        self._execution_user_id = execution_user_id

    async def create_run_with_initial_steps(
        self, run: PlanningRun, steps: tuple[AgentStep, ...]
    ) -> RunCreationResult:
        if run.status is not PlanningRunStatus.CREATED:
            raise InvalidRunStateTransition("A new run must start in CREATED.")
        if not steps or any(step.run_id != run.id for step in steps):
            raise DomainValidationError("Initial steps must belong to the new run.")
        if len({step.id for step in steps}) != len(steps):
            raise DomainValidationError("Initial step ids must be unique.")
        async with self.session_factory() as session:
            try:
                async with session.begin():
                    existing = await session.scalar(
                        select(PlanningRunModel)
                        .where(
                            PlanningRunModel.user_id == str(run.user_id),
                            PlanningRunModel.workflow_type == run.workflow_type.value,
                            PlanningRunModel.client_request_id == run.client_request_id,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        if existing.request_fingerprint != run.request_fingerprint:
                            raise RunIdempotencyConflict(
                                "The client request id belongs to another payload."
                            )
                        return RunCreationResult(
                            run=self._run_from_row(existing), created=False
                        )
                    queued = replace(
                        transition_run(run, PlanningRunStatus.QUEUED),
                        current_step_id=steps[0].id,
                        updated_at=run.created_at,
                        version=run.version + 1,
                    )
                    run_row = self._run_row(queued)
                    session.add(run_row)
                    await session.flush()
                    for step in sorted(steps, key=self._step_sort_key):
                        session.add(self._step_row(step))
                    await session.flush()
                    for step in steps:
                        for dependency_id in step.dependency_step_ids:
                            session.add(
                                StepDependencyModel(
                                    step_id=str(step.id),
                                    dependency_step_id=str(dependency_id),
                                )
                            )
                    await self._append_audit(
                        session,
                        run_id=run.id,
                        event_type=AuditEventType.RUN_CREATED,
                        now=run.created_at,
                        to_status=PlanningRunStatus.CREATED.value,
                    )
                    await self._append_audit(
                        session,
                        run_id=run.id,
                        event_type=AuditEventType.RUN_STATUS_CHANGED,
                        now=run.created_at,
                        from_status=PlanningRunStatus.CREATED.value,
                        to_status=PlanningRunStatus.QUEUED.value,
                    )
                    for step in sorted(steps, key=self._step_sort_key):
                        await self._append_audit(
                            session,
                            run_id=run.id,
                            step_id=step.id,
                            event_type=AuditEventType.STEP_CREATED,
                            now=run.created_at,
                            to_status=step.status.value,
                            metadata={"step_type": step.step_type.value},
                        )
                    await session.refresh(run_row)
                    return RunCreationResult(
                        run=self._run_from_row(run_row), created=True
                    )
            except IntegrityError as exc:
                raise RunIdempotencyConflict(
                    "Planning run identity already contains another payload."
                ) from exc

    async def get_run(self, run_id: UUID) -> PlanningRun | None:
        async with self.session_factory() as session:
            row = await session.get(PlanningRunModel, str(run_id))
            return None if row is None else self._run_from_row(row)

    async def get_run_for_user(self, run_id: UUID, user_id: UUID) -> PlanningRun | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(PlanningRunModel).where(
                    PlanningRunModel.id == str(run_id),
                    PlanningRunModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._run_from_row(row)

    async def list_steps(self, run_id: UUID) -> list[AgentStep]:
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(AgentStepModel)
                    .where(AgentStepModel.run_id == str(run_id))
                    .order_by(
                        AgentStepModel.priority.desc(),
                        AgentStepModel.next_execute_at,
                        AgentStepModel.sequence_no,
                        AgentStepModel.created_at,
                        AgentStepModel.id,
                    )
                )
            ).all()
            return [await self._step_from_row(session, row) for row in rows]

    async def claim_next_step(
        self, *, worker_id: str, lease_duration: timedelta, now: datetime
    ) -> ClaimedStep | None:
        if not worker_id.strip() or lease_duration.total_seconds() <= 0:
            raise ValueError("worker_id and lease_duration must be valid.")
        for lock_attempt in range(_CLAIM_LOCK_ERROR_ATTEMPTS):
            try:
                for availability_attempt in range(_CLAIM_AVAILABILITY_ATTEMPTS):
                    claimed = await self._claim_next_step_once(
                        worker_id=worker_id,
                        lease_duration=lease_duration,
                        now=now,
                    )
                    if (
                        claimed is not None
                        or availability_attempt == _CLAIM_AVAILABILITY_ATTEMPTS - 1
                    ):
                        return claimed
                    await asyncio.sleep(0.01 * (availability_attempt + 1))
            except OperationalError as exc:
                if (
                    not self._is_retryable_lock_error(exc)
                    or lock_attempt == _CLAIM_LOCK_ERROR_ATTEMPTS - 1
                ):
                    raise
                await asyncio.sleep(0)
        raise RuntimeError("Bounded claim retry loop exhausted unexpectedly.")

    async def _claim_next_step_once(
        self, *, worker_id: str, lease_duration: timedelta, now: datetime
    ) -> ClaimedStep | None:
        now_db = self._db_time(now)
        dependency = aliased(AgentStepModel)
        eligible_runs = select(PlanningRunModel.id).where(
            PlanningRunModel.status.not_in(
                [item.value for item in RUN_TERMINAL_STATUSES]
            )
        )
        if self._execution_user_id is not None:
            eligible_runs = eligible_runs.where(
                PlanningRunModel.user_id == str(self._execution_user_id)
            )
        blocked_dependency = (
            select(1)
            .select_from(StepDependencyModel)
            .join(dependency, dependency.id == StepDependencyModel.dependency_step_id)
            .where(
                StepDependencyModel.step_id == AgentStepModel.id,
                dependency.status != AgentStepStatus.SUCCEEDED.value,
            )
        )
        async with self._claim_session_factory()() as session:
            async with session.begin():
                step_rows = (
                    await session.scalars(
                        select(AgentStepModel)
                        .where(
                            AgentStepModel.status.in_(
                                [
                                    AgentStepStatus.READY.value,
                                    AgentStepStatus.RETRY_SCHEDULED.value,
                                ]
                            ),
                            AgentStepModel.next_execute_at <= now_db,
                            AgentStepModel.run_id.in_(eligible_runs),
                            ~exists(blocked_dependency),
                        )
                        .order_by(
                            AgentStepModel.priority.desc(),
                            AgentStepModel.next_execute_at,
                            AgentStepModel.sequence_no,
                            AgentStepModel.created_at,
                            AgentStepModel.id,
                        )
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                step_row = next(iter(step_rows), None)
                if step_row is None:
                    return None
                run_row = await session.scalar(
                    select(PlanningRunModel)
                    .where(PlanningRunModel.id == step_row.run_id)
                    .with_for_update()
                )
                if run_row is None:
                    raise StepNotFound("Step run was not found.")
                previous_step_status = AgentStepStatus(step_row.status)
                step = await self._step_from_row(session, step_row)
                if step.status is AgentStepStatus.RETRY_SCHEDULED:
                    step = transition_step(step, AgentStepStatus.READY)
                step = replace(
                    transition_step(step, AgentStepStatus.RUNNING),
                    attempt_count=step.attempt_count + 1,
                    fencing_token=step.fencing_token + 1,
                    worker_id=worker_id,
                    lease_token=uuid4(),
                    lease_expires_at=now + lease_duration,
                    heartbeat_at=now,
                    updated_at=now,
                    version=step.version + 1,
                )
                self._apply_step(step_row, step)
                run = self._run_from_row(run_row)
                target = _CLAIM_RUN_STATUS[step.step_type]
                previous_run_status = run.status
                if run.status is not target:
                    run = transition_run(run, target)
                run = replace(
                    run,
                    current_step_id=step.id,
                    updated_at=now,
                    version=run.version + 1,
                )
                self._apply_run(run_row, run)
                await self._append_audit(
                    session,
                    run_id=run.id,
                    step_id=step.id,
                    event_type=AuditEventType.STEP_CLAIMED,
                    now=now,
                    from_status=previous_step_status.value,
                    to_status=step.status.value,
                    worker_id=worker_id,
                    attempt_no=step.attempt_count,
                )
                if previous_run_status is not run.status:
                    await self._append_audit(
                        session,
                        run_id=run.id,
                        event_type=AuditEventType.RUN_STATUS_CHANGED,
                        now=now,
                        from_status=previous_run_status.value,
                        to_status=run.status.value,
                    )
                return ClaimedStep(run=run, step=step)

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
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._locked_step(session, step_id)
                current = await self._validate_lease(
                    session, row, worker_id, lease_token, now
                )
                if current.fencing_token != fencing_token:
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
                self._apply_step(row, updated)
                await self._append_audit(
                    session,
                    run_id=updated.run_id,
                    step_id=updated.id,
                    event_type=AuditEventType.STEP_HEARTBEAT,
                    now=now,
                    from_status=updated.status.value,
                    to_status=updated.status.value,
                    worker_id=worker_id,
                    attempt_no=updated.attempt_count,
                )
                return updated

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
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._locked_step(session, claim.step.id)
                current = await self._validate_claim(session, row, claim, now)
                run_row = await self._locked_run(session, current.run_id)
                run = self._run_from_row(run_row)
                if current.status is AgentStepStatus.SUCCEEDED:
                    return current
                completed = replace(
                    transition_step(current, AgentStepStatus.SUCCEEDED),
                    output_payload=payload,
                    worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    completed_at=now,
                    updated_at=now,
                    version=current.version + 1,
                )
                self._apply_step(row, completed)
                previous_run_status = run.status
                if run.status is not run_status_after:
                    run = transition_run(run, run_status_after)
                next_step = await self._create_next_step(
                    session,
                    current=completed,
                    next_step_type=next_step_type,
                    payload=payload,
                    now=now,
                )
                run = replace(
                    run,
                    result_reference=result_reference or run.result_reference,
                    current_step_id=None if next_step is None else next_step.id,
                    completed_at=(
                        now if run.status is PlanningRunStatus.COMPLETED else None
                    ),
                    updated_at=now,
                    version=run.version + 1,
                )
                self._apply_run(run_row, run)
                checkpoint = self._checkpoint_for(
                    completed,
                    handler_version,
                    payload,
                    result_reference,
                    run.status,
                    now,
                )
                await self._save_checkpoint(session, checkpoint)
                await self._append_audit(
                    session,
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
                    await self._append_audit(
                        session,
                        run_id=run.id,
                        event_type=AuditEventType.RUN_STATUS_CHANGED,
                        now=now,
                        from_status=previous_run_status.value,
                        to_status=run.status.value,
                    )
                if next_step is not None:
                    await self._append_audit(
                        session,
                        run_id=run.id,
                        step_id=next_step.id,
                        event_type=AuditEventType.STEP_CREATED,
                        now=now,
                        to_status=next_step.status.value,
                        metadata={"step_type": next_step.step_type.value},
                    )
                return completed

    async def fail_step(
        self,
        *,
        claim: ClaimedStep,
        error_code: str,
        error_message: str,
        retry_at: datetime | None,
        now: datetime,
    ) -> AgentStep:
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._locked_step(session, claim.step.id)
                current = await self._validate_claim(session, row, claim, now)
                run_row = await self._locked_run(session, current.run_id)
                run = self._run_from_row(run_row)
                previous_run_status = run.status
                if (
                    retry_at is not None
                    and current.attempt_count < current.max_attempts
                ):
                    updated = transition_step(current, AgentStepStatus.FAILED_RETRYABLE)
                    updated = transition_step(updated, AgentStepStatus.RETRY_SCHEDULED)
                    target_status = PlanningRunStatus.FAILED_RETRYABLE
                    event_type = AuditEventType.STEP_RETRY_SCHEDULED
                    completed_at = None
                else:
                    updated = transition_step(current, AgentStepStatus.FAILED_PERMANENT)
                    target_status = PlanningRunStatus.FAILED_PERMANENT
                    event_type = AuditEventType.STEP_FAILED_PERMANENT
                    completed_at = now
                updated = replace(
                    updated,
                    next_execute_at=now if retry_at is None else retry_at,
                    worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    last_error_code=error_code[:128],
                    last_error_message=self._safe_error(error_message),
                    completed_at=completed_at,
                    updated_at=now,
                    version=current.version + 1,
                )
                self._apply_step(row, updated)
                if run.status is not target_status:
                    run = transition_run(run, target_status)
                run = replace(
                    run,
                    current_step_id=updated.id,
                    updated_at=now,
                    version=run.version + 1,
                )
                self._apply_run(run_row, run)
                await self._append_audit(
                    session,
                    run_id=run.id,
                    step_id=updated.id,
                    event_type=event_type,
                    now=now,
                    from_status=AgentStepStatus.RUNNING.value,
                    to_status=updated.status.value,
                    worker_id=claim.step.worker_id,
                    attempt_no=updated.attempt_count,
                    error_code=updated.last_error_code,
                )
                if previous_run_status is not run.status:
                    await self._append_audit(
                        session,
                        run_id=run.id,
                        event_type=AuditEventType.RUN_STATUS_CHANGED,
                        now=now,
                        from_status=previous_run_status.value,
                        to_status=run.status.value,
                    )
                return updated

    async def mark_waiting_user(
        self,
        *,
        claim: ClaimedStep,
        output_payload: JsonObject,
        run_status_after: PlanningRunStatus,
        now: datetime,
    ) -> AgentStep:
        payload = safe_json_object(output_payload)
        async with self.session_factory() as session:
            async with session.begin():
                row = await self._locked_step(session, claim.step.id)
                current = await self._validate_claim(session, row, claim, now)
                run_row = await self._locked_run(session, current.run_id)
                run = self._run_from_row(run_row)
                previous_run_status = run.status
                waiting = replace(
                    transition_step(current, AgentStepStatus.WAITING_USER),
                    output_payload=payload,
                    worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    updated_at=now,
                    version=current.version + 1,
                )
                self._apply_step(row, waiting)
                if run.status is not run_status_after:
                    run = transition_run(run, run_status_after)
                run = replace(
                    run,
                    current_step_id=waiting.id,
                    updated_at=now,
                    version=run.version + 1,
                )
                self._apply_run(run_row, run)
                await self._append_audit(
                    session,
                    run_id=run.id,
                    step_id=waiting.id,
                    event_type=AuditEventType.STEP_WAITING_USER,
                    now=now,
                    from_status=AgentStepStatus.RUNNING.value,
                    to_status=waiting.status.value,
                    worker_id=claim.step.worker_id,
                    attempt_no=waiting.attempt_count,
                )
                if previous_run_status is not run.status:
                    await self._append_audit(
                        session,
                        run_id=run.id,
                        event_type=AuditEventType.RUN_STATUS_CHANGED,
                        now=now,
                        from_status=previous_run_status.value,
                        to_status=run.status.value,
                    )
                return waiting

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
        supplied = None if resume_payload is None else safe_json_object(resume_payload)
        async with self.session_factory() as session:
            async with session.begin():
                run_row = await self._locked_run(session, run_id)
                row = await self._locked_step(session, expected_step_id)
                current = await self._step_from_row(session, row)
                if current.run_id != run_id:
                    raise StepNotFound("Waiting step was not found.")
                if current.status is AgentStepStatus.SUCCEEDED:
                    if supplied is not None and current.output_payload != supplied:
                        raise CheckpointConflict(
                            "The waiting step has another payload."
                        )
                    return current
                waiting_ids = (
                    await session.scalars(
                        select(AgentStepModel.id)
                        .where(
                            AgentStepModel.run_id == str(run_id),
                            AgentStepModel.status == AgentStepStatus.WAITING_USER.value,
                        )
                        .with_for_update()
                    )
                ).all()
                if len(waiting_ids) != 1 or waiting_ids[0] != str(expected_step_id):
                    raise MultipleWaitingSteps(
                        "Run must contain exactly one waiting step."
                    )
                payload = supplied or current.output_payload or current.input_payload
                completed = replace(
                    transition_step(current, AgentStepStatus.SUCCEEDED),
                    output_payload=payload,
                    completed_at=now,
                    updated_at=now,
                    version=current.version + 1,
                )
                self._apply_step(row, completed)
                next_step = await self._create_next_step(
                    session,
                    current=completed,
                    next_step_type=next_step_type,
                    payload=payload,
                    now=now,
                )
                if next_step is None:
                    raise DomainValidationError("Resume requires a next step.")
                run = replace(
                    self._run_from_row(run_row),
                    current_step_id=next_step.id,
                    updated_at=now,
                    version=run_row.version + 1,
                )
                self._apply_run(run_row, run)
                await self._save_checkpoint(
                    session,
                    self._checkpoint_for(
                        completed,
                        handler_version,
                        payload,
                        run.result_reference,
                        run.status,
                        now,
                    ),
                )
                await self._append_audit(
                    session,
                    run_id=run.id,
                    step_id=completed.id,
                    event_type=AuditEventType.STEP_RESUMED,
                    now=now,
                    from_status=AgentStepStatus.WAITING_USER.value,
                    to_status=completed.status.value,
                    attempt_no=completed.attempt_count,
                )
                await self._append_audit(
                    session,
                    run_id=run.id,
                    step_id=next_step.id,
                    event_type=AuditEventType.STEP_CREATED,
                    now=now,
                    to_status=next_step.status.value,
                    metadata={"step_type": next_step.step_type.value},
                )
                return completed

    async def reap_expired_steps(
        self, *, now: datetime, delays_seconds: tuple[int, ...]
    ) -> list[AgentStep]:
        if not delays_seconds:
            raise ValueError("delays_seconds must not be empty.")
        reaped: list[AgentStep] = []
        async with self.session_factory() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(AgentStepModel)
                        .join(
                            PlanningRunModel,
                            PlanningRunModel.id == AgentStepModel.run_id,
                        )
                        .where(
                            AgentStepModel.status == AgentStepStatus.RUNNING.value,
                            AgentStepModel.lease_expires_at < self._db_time(now),
                            *(
                                ()
                                if self._execution_user_id is None
                                else (
                                    PlanningRunModel.user_id
                                    == str(self._execution_user_id),
                                )
                            ),
                        )
                        .order_by(
                            AgentStepModel.priority.desc(),
                            AgentStepModel.next_execute_at,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for row in rows:
                    current = await self._step_from_row(session, row)
                    run_row = await self._locked_run(session, current.run_id)
                    run = self._run_from_row(run_row)
                    previous_run_status = run.status
                    if current.attempt_count >= current.max_attempts:
                        updated = replace(
                            transition_step(current, AgentStepStatus.FAILED_PERMANENT),
                            last_error_code="MAX_ATTEMPTS_EXCEEDED",
                            last_error_message=(
                                "Lease expired at the maximum attempt count."
                            ),
                            completed_at=now,
                        )
                        target = PlanningRunStatus.FAILED_PERMANENT
                    else:
                        retry = transition_step(
                            current, AgentStepStatus.FAILED_RETRYABLE
                        )
                        updated = replace(
                            transition_step(retry, AgentStepStatus.RETRY_SCHEDULED),
                            next_execute_at=now
                            + timedelta(
                                seconds=delays_seconds[
                                    min(
                                        current.attempt_count - 1,
                                        len(delays_seconds) - 1,
                                    )
                                ]
                            ),
                            last_error_code="STEP_LEASE_EXPIRED",
                            last_error_message=(
                                "Worker lease expired before completion."
                            ),
                        )
                        target = PlanningRunStatus.FAILED_RETRYABLE
                    updated = replace(
                        updated,
                        worker_id=None,
                        lease_token=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                        updated_at=now,
                        version=current.version + 1,
                    )
                    self._apply_step(row, updated)
                    if run.status is not target:
                        run = transition_run(run, target)
                    run = replace(
                        run,
                        current_step_id=updated.id,
                        updated_at=now,
                        version=run.version + 1,
                    )
                    self._apply_run(run_row, run)
                    await self._append_audit(
                        session,
                        run_id=run.id,
                        step_id=updated.id,
                        event_type=AuditEventType.STEP_REAPED,
                        now=now,
                        from_status=AgentStepStatus.RUNNING.value,
                        to_status=updated.status.value,
                        attempt_no=updated.attempt_count,
                    )
                    if previous_run_status is not run.status:
                        await self._append_audit(
                            session,
                            run_id=run.id,
                            event_type=AuditEventType.RUN_STATUS_CHANGED,
                            now=now,
                            from_status=previous_run_status.value,
                            to_status=run.status.value,
                        )
                    reaped.append(updated)
        return reaped

    async def cancel_run(self, *, run_id: UUID, now: datetime) -> PlanningRun:
        async with self.session_factory() as session:
            async with session.begin():
                run_row = await self._locked_run(session, run_id)
                run = self._run_from_row(run_row)
                if run.status is PlanningRunStatus.CANCELLED:
                    return run
                if run.status in RUN_TERMINAL_STATUSES:
                    raise InvalidRunStateTransition(
                        "A terminal run cannot be cancelled."
                    )
                previous = run.status
                run = replace(
                    transition_run(run, PlanningRunStatus.CANCELLED),
                    current_step_id=None,
                    completed_at=now,
                    updated_at=now,
                    version=run.version + 1,
                )
                self._apply_run(run_row, run)
                rows = (
                    await session.scalars(
                        select(AgentStepModel)
                        .where(AgentStepModel.run_id == str(run_id))
                        .with_for_update()
                    )
                ).all()
                for row in rows:
                    step = await self._step_from_row(session, row)
                    if step.status in STEP_TERMINAL_STATUSES:
                        continue
                    cancelled = replace(
                        transition_step(step, AgentStepStatus.CANCELLED),
                        worker_id=None,
                        lease_token=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                        completed_at=now,
                        updated_at=now,
                        version=step.version + 1,
                    )
                    self._apply_step(row, cancelled)
                    await self._append_audit(
                        session,
                        run_id=run.id,
                        step_id=step.id,
                        event_type=AuditEventType.STEP_CANCELLED,
                        now=now,
                        from_status=step.status.value,
                        to_status=cancelled.status.value,
                    )
                await self._append_audit(
                    session,
                    run_id=run.id,
                    event_type=AuditEventType.RUN_STATUS_CHANGED,
                    now=now,
                    from_status=previous.value,
                    to_status=run.status.value,
                )
                await self._append_audit(
                    session,
                    run_id=run.id,
                    event_type=AuditEventType.RUN_CANCELLED,
                    now=now,
                    from_status=previous.value,
                    to_status=run.status.value,
                )
                return run

    async def list_checkpoints(self, run_id: UUID) -> list[StepCheckpoint]:
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(CheckpointModel)
                    .where(CheckpointModel.run_id == str(run_id))
                    .order_by(
                        CheckpointModel.created_at,
                        CheckpointModel.step_id,
                        CheckpointModel.attempt_no,
                    )
                )
            ).all()
            return [self._checkpoint_from_row(row) for row in rows]

    async def list_audit_events(self, run_id: UUID) -> list[OrchestrationAuditEvent]:
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(AuditEventModel)
                    .where(
                        AuditEventModel.run_id == str(run_id),
                        AuditEventModel.sequence_no.is_not(None),
                        AuditEventModel.event_type.in_(
                            tuple(item.value for item in AuditEventType)
                        ),
                    )
                    .order_by(AuditEventModel.sequence_no, AuditEventModel.id)
                )
            ).all()
            return [self._audit_from_row(row) for row in rows]

    async def append_audit_event(
        self, event: OrchestrationAuditEvent
    ) -> OrchestrationAuditEvent:
        async with self.session_factory() as session:
            async with session.begin():
                run = await self._locked_run(session, event.run_id)
                next_sequence = await self._next_audit_sequence(session, event.run_id)
                if event.sequence_no != next_sequence:
                    raise ValueError("Audit sequence must be contiguous per run.")
                session.add(self._audit_row(event, run.user_id))
            return event

    async def save_checkpoint(self, checkpoint: StepCheckpoint) -> StepCheckpoint:
        async with self.session_factory() as session:
            async with session.begin():
                await self._save_checkpoint(session, checkpoint)
            return checkpoint

    async def reset(self) -> None:
        """Delete only isolated-test orchestration rows, never shared business data."""

        bind = self.session_factory.kw.get("bind")
        database = (
            "" if bind is None or bind.url.database is None else bind.url.database
        )
        if not database.endswith("_test"):
            raise RuntimeError("reset is restricted to an isolated *_test database.")
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(delete(AuditEventModel))
                await session.execute(delete(CheckpointModel))
                await session.execute(delete(StepDependencyModel))
                await session.execute(delete(AgentStepModel))
                await session.execute(delete(PlanningRunModel))

    async def _locked_step(
        self, session: AsyncSession, step_id: UUID
    ) -> AgentStepModel:
        row = await session.scalar(
            select(AgentStepModel)
            .where(AgentStepModel.id == str(step_id))
            .with_for_update()
        )
        if row is None:
            raise StepNotFound("Step was not found.")
        return row

    async def _locked_run(
        self, session: AsyncSession, run_id: UUID
    ) -> PlanningRunModel:
        row = await session.scalar(
            select(PlanningRunModel)
            .where(PlanningRunModel.id == str(run_id))
            .with_for_update()
        )
        if row is None:
            raise StepNotFound("Run was not found.")
        return row

    async def _validate_claim(
        self,
        session: AsyncSession,
        row: AgentStepModel,
        claim: ClaimedStep,
        now: datetime,
    ) -> AgentStep:
        worker_id = claim.step.worker_id
        lease_token = claim.step.lease_token
        if worker_id is None or lease_token is None:
            raise StepLeaseLost("Claim has no active lease ownership.")
        current = await self._validate_lease(session, row, worker_id, lease_token, now)
        if current.fencing_token != claim.step.fencing_token:
            raise StepLeaseLost("Worker no longer owns the active fencing generation.")
        return current

    async def _validate_lease(
        self,
        session: AsyncSession,
        row: AgentStepModel,
        worker_id: str,
        lease_token: UUID,
        now: datetime,
    ) -> AgentStep:
        current = await self._step_from_row(session, row)
        if (
            current.status is not AgentStepStatus.RUNNING
            or current.worker_id != worker_id
            or current.lease_token != lease_token
            or current.lease_expires_at is None
            or current.lease_expires_at < now
        ):
            raise StepLeaseLost("Worker no longer owns the active step lease.")
        return current

    async def _create_next_step(
        self,
        session: AsyncSession,
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
            _STEP_ID_NAMESPACE, f"{current.run_id}:{sequence_no}:{next_step_type.value}"
        )
        existing = await session.get(AgentStepModel, str(step_id))
        if existing is not None:
            return await self._step_from_row(session, existing)
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
        session.add(self._step_row(next_step))
        session.add(
            StepDependencyModel(
                step_id=str(next_step.id), dependency_step_id=str(current.id)
            )
        )
        return next_step

    async def _save_checkpoint(
        self, session: AsyncSession, checkpoint: StepCheckpoint
    ) -> None:
        existing = await session.scalar(
            select(CheckpointModel)
            .where(
                CheckpointModel.step_id == str(checkpoint.step_id),
                CheckpointModel.attempt_no == checkpoint.attempt_no,
            )
            .with_for_update()
        )
        if existing is not None:
            raise CheckpointConflict("A checkpoint already exists for this attempt.")
        session.add(self._checkpoint_row(checkpoint))

    async def _append_audit(
        self,
        session: AsyncSession,
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
        sequence_no = await self._next_audit_sequence(session, run_id)
        session.add(
            AuditEventModel(
                id=str(uuid4()),
                run_id=str(run_id),
                step_id=None if step_id is None else str(step_id),
                user_id=(await self._locked_run(session, run_id)).user_id,
                sequence_no=sequence_no,
                event_type=event_type.value,
                from_status=from_status,
                to_status=to_status,
                worker_id=worker_id,
                attempt_no=attempt_no,
                error_code=error_code,
                event_metadata=safe_json_object(metadata or {}),
                occurred_at=self._db_time(now),
            )
        )

    async def _next_audit_sequence(self, session: AsyncSession, run_id: UUID) -> int:
        value = await session.scalar(
            select(func.max(AuditEventModel.sequence_no)).where(
                AuditEventModel.run_id == str(run_id)
            )
        )
        return int(value or 0) + 1

    def _claim_session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Use session-local READ COMMITTED for non-blocking claim scans."""

        bind = self.session_factory.kw.get("bind")
        if bind is None:
            return self.session_factory
        return async_sessionmaker(
            bind=bind.execution_options(isolation_level="READ COMMITTED"),
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @staticmethod
    def _is_retryable_lock_error(error: OperationalError) -> bool:
        arguments = getattr(error.orig, "args", ())
        return bool(arguments and arguments[0] in {1205, 1213})

    @classmethod
    async def _step_from_row(
        cls, session: AsyncSession, row: AgentStepModel
    ) -> AgentStep:
        dependency_ids = (
            await session.scalars(
                select(StepDependencyModel.dependency_step_id).where(
                    StepDependencyModel.step_id == row.id
                )
            )
        ).all()
        return AgentStep(
            id=UUID(row.id),
            run_id=UUID(row.run_id),
            step_type=StepType(row.step_type),
            status=AgentStepStatus(row.status),
            sequence_no=row.sequence_no,
            priority=row.priority,
            input_payload=safe_json_object(row.input_payload),
            output_payload=(
                None
                if row.output_payload is None
                else safe_json_object(row.output_payload)
            ),
            dependency_step_ids=tuple(UUID(item) for item in dependency_ids),
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            next_execute_at=cls._utc(row.next_execute_at),
            worker_id=row.worker_id,
            lease_token=None if row.lease_token is None else UUID(row.lease_token),
            lease_expires_at=None
            if row.lease_expires_at is None
            else cls._utc(row.lease_expires_at),
            heartbeat_at=None
            if row.heartbeat_at is None
            else cls._utc(row.heartbeat_at),
            last_error_code=row.last_error_code,
            last_error_message=row.last_error_message,
            created_at=cls._utc(row.created_at),
            updated_at=cls._utc(row.updated_at),
            completed_at=None
            if row.completed_at is None
            else cls._utc(row.completed_at),
            version=row.version,
            fencing_token=row.fencing_token,
        )

    @classmethod
    def _run_from_row(cls, row: PlanningRunModel) -> PlanningRun:
        return PlanningRun(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            workflow_type=WorkflowType(row.workflow_type),
            status=PlanningRunStatus(row.status),
            request_fingerprint=row.request_fingerprint,
            input_payload=safe_json_object(row.input_payload),
            result_reference=row.result_reference,
            current_step_id=None
            if row.current_step_id is None
            else UUID(row.current_step_id),
            created_at=cls._utc(row.created_at),
            updated_at=cls._utc(row.updated_at),
            completed_at=None
            if row.completed_at is None
            else cls._utc(row.completed_at),
            version=row.version,
            client_request_id=row.client_request_id,
        )

    @classmethod
    def _checkpoint_from_row(cls, row: CheckpointModel) -> StepCheckpoint:
        return StepCheckpoint(
            id=UUID(row.id),
            run_id=UUID(row.run_id),
            step_id=UUID(row.step_id),
            attempt_no=row.attempt_no,
            step_type=StepType(row.step_type),
            handler_version=row.handler_version,
            input_fingerprint=row.input_fingerprint,
            output_payload=safe_json_object(row.output_payload),
            result_reference=row.result_reference,
            run_status_after=PlanningRunStatus(row.run_status_after),
            step_status_after=AgentStepStatus(row.step_status_after),
            created_at=cls._utc(row.created_at),
        )

    @classmethod
    def _audit_from_row(cls, row: AuditEventModel) -> OrchestrationAuditEvent:
        if row.sequence_no is None:
            raise ValueError(
                "Stored orchestration audit event lacks a sequence number."
            )
        return OrchestrationAuditEvent(
            id=UUID(row.id),
            sequence_no=row.sequence_no,
            run_id=UUID(row.run_id),
            step_id=None if row.step_id is None else UUID(row.step_id),
            event_type=AuditEventType(row.event_type),
            from_status=row.from_status,
            to_status=row.to_status,
            worker_id=row.worker_id,
            attempt_no=row.attempt_no,
            error_code=row.error_code,
            metadata=safe_json_object(row.event_metadata),
            occurred_at=cls._utc(row.occurred_at),
        )

    @classmethod
    def _run_row(cls, value: PlanningRun) -> PlanningRunModel:
        return PlanningRunModel(
            id=str(value.id),
            user_id=str(value.user_id),
            workflow_type=value.workflow_type.value,
            status=value.status.value,
            client_request_id=value.client_request_id,
            request_fingerprint=value.request_fingerprint,
            result_reference=value.result_reference,
            current_step_id=None
            if value.current_step_id is None
            else str(value.current_step_id),
            input_payload=safe_json_object(value.input_payload),
            created_at=cls._db_time(value.created_at),
            updated_at=cls._db_time(value.updated_at),
            completed_at=None
            if value.completed_at is None
            else cls._db_time(value.completed_at),
            version=value.version,
        )

    @classmethod
    def _step_row(cls, value: AgentStep) -> AgentStepModel:
        return AgentStepModel(
            id=str(value.id),
            run_id=str(value.run_id),
            step_type=value.step_type.value,
            status=value.status.value,
            sequence_no=value.sequence_no,
            priority=value.priority,
            input_payload=safe_json_object(value.input_payload),
            output_payload=None
            if value.output_payload is None
            else safe_json_object(value.output_payload),
            attempt_count=value.attempt_count,
            max_attempts=value.max_attempts,
            next_execute_at=cls._db_time(value.next_execute_at),
            worker_id=value.worker_id,
            lease_token=None if value.lease_token is None else str(value.lease_token),
            fencing_token=value.fencing_token,
            lease_expires_at=None
            if value.lease_expires_at is None
            else cls._db_time(value.lease_expires_at),
            heartbeat_at=None
            if value.heartbeat_at is None
            else cls._db_time(value.heartbeat_at),
            last_error_code=value.last_error_code,
            last_error_message=value.last_error_message,
            created_at=cls._db_time(value.created_at),
            updated_at=cls._db_time(value.updated_at),
            completed_at=None
            if value.completed_at is None
            else cls._db_time(value.completed_at),
            version=value.version,
        )

    @classmethod
    def _checkpoint_row(cls, value: StepCheckpoint) -> CheckpointModel:
        return CheckpointModel(
            id=str(value.id),
            run_id=str(value.run_id),
            step_id=str(value.step_id),
            attempt_no=value.attempt_no,
            step_type=value.step_type.value,
            handler_version=value.handler_version,
            input_fingerprint=value.input_fingerprint,
            output_payload=safe_json_object(value.output_payload),
            result_reference=value.result_reference,
            run_status_after=value.run_status_after.value,
            step_status_after=value.step_status_after.value,
            created_at=cls._db_time(value.created_at),
        )

    @classmethod
    def _audit_row(
        cls, value: OrchestrationAuditEvent, user_id: str
    ) -> AuditEventModel:
        return AuditEventModel(
            id=str(value.id),
            user_id=user_id,
            run_id=str(value.run_id),
            step_id=None if value.step_id is None else str(value.step_id),
            sequence_no=value.sequence_no,
            event_type=value.event_type.value,
            from_status=value.from_status,
            to_status=value.to_status,
            worker_id=value.worker_id,
            attempt_no=value.attempt_no,
            error_code=value.error_code,
            event_metadata=safe_json_object(value.metadata),
            occurred_at=cls._db_time(value.occurred_at),
        )

    @classmethod
    def _apply_run(cls, row: PlanningRunModel, value: PlanningRun) -> None:
        for key, item in cls._run_row(value).__dict__.items():
            if not key.startswith("_"):
                setattr(row, key, item)

    @classmethod
    def _apply_step(cls, row: AgentStepModel, value: AgentStep) -> None:
        for key, item in cls._step_row(value).__dict__.items():
            if not key.startswith("_"):
                setattr(row, key, item)

    @staticmethod
    def _step_sort_key(value: AgentStep) -> tuple[int, datetime, int, datetime, str]:
        return (
            -value.priority,
            value.next_execute_at,
            value.sequence_no,
            value.created_at,
            str(value.id),
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)

    @staticmethod
    def _safe_error(value: str) -> str:
        compact = " ".join(str(value).split())[:240]
        lowered = compact.casefold()
        if "://" in compact or any(
            item in lowered
            for item in ("password=", "database_url", "redis_url", "token=")
        ):
            return "Handler failed; sensitive details were redacted."
        return compact or "Handler failed without a client-safe message."

    @staticmethod
    def _validate_reference(value: str | None) -> None:
        if value is not None and (not value.strip() or "://" in value):
            raise DomainValidationError("result_reference must be a safe opaque value.")

    @staticmethod
    def _checkpoint_for(
        step: AgentStep,
        handler_version: str,
        output_payload: JsonObject,
        result_reference: str | None,
        run_status_after: PlanningRunStatus,
        now: datetime,
    ) -> StepCheckpoint:
        fingerprint = hashlib.sha256(
            json.dumps(
                step.input_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return StepCheckpoint(
            id=uuid5(_CHECKPOINT_ID_NAMESPACE, f"{step.id}:{step.attempt_count}"),
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
