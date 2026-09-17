"""Persistence-independent atomic orchestration repository contract."""

from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.domain.orchestration.enums import PlanningRunStatus, StepType
from app.domain.orchestration.models import (
    AgentStep,
    ClaimedStep,
    JsonObject,
    OrchestrationAuditEvent,
    PlanningRun,
    RunCreationResult,
    StepCheckpoint,
)


class OrchestrationRepository(Protocol):
    async def create_run_with_initial_steps(
        self, run: PlanningRun, steps: tuple[AgentStep, ...]
    ) -> RunCreationResult: ...

    async def get_run(self, run_id: UUID) -> PlanningRun | None: ...

    async def get_run_for_user(
        self, run_id: UUID, user_id: UUID
    ) -> PlanningRun | None: ...

    async def list_steps(self, run_id: UUID) -> list[AgentStep]: ...

    async def claim_next_step(
        self, *, worker_id: str, lease_duration: timedelta, now: datetime
    ) -> ClaimedStep | None: ...

    async def heartbeat(
        self,
        *,
        step_id: UUID,
        worker_id: str,
        lease_token: UUID,
        fencing_token: int,
        lease_duration: timedelta,
        now: datetime,
    ) -> AgentStep: ...

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
    ) -> AgentStep: ...

    async def fail_step(
        self,
        *,
        claim: ClaimedStep,
        error_code: str,
        error_message: str,
        retry_at: datetime | None,
        now: datetime,
    ) -> AgentStep: ...

    async def mark_waiting_user(
        self,
        *,
        claim: ClaimedStep,
        output_payload: JsonObject,
        run_status_after: PlanningRunStatus,
        now: datetime,
    ) -> AgentStep: ...

    async def resume_waiting_step(
        self,
        *,
        run_id: UUID,
        expected_step_id: UUID,
        handler_version: str,
        next_step_type: StepType,
        now: datetime,
        resume_payload: JsonObject | None = None,
    ) -> AgentStep: ...

    async def reap_expired_steps(
        self,
        *,
        now: datetime,
        delays_seconds: tuple[int, ...],
    ) -> list[AgentStep]: ...

    async def cancel_run(self, *, run_id: UUID, now: datetime) -> PlanningRun: ...

    async def list_checkpoints(self, run_id: UUID) -> list[StepCheckpoint]: ...

    async def list_audit_events(
        self, run_id: UUID
    ) -> list[OrchestrationAuditEvent]: ...

    async def append_audit_event(
        self, event: OrchestrationAuditEvent
    ) -> OrchestrationAuditEvent: ...

    async def save_checkpoint(self, checkpoint: StepCheckpoint) -> StepCheckpoint: ...

    async def reset(self) -> None: ...
