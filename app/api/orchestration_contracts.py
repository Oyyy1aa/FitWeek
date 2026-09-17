"""Stable request and response DTOs for the Phase 2A control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.contracts import GenerateWeeklyPlanRequest, RequestModel
from app.domain.orchestration.enums import (
    AgentStepStatus,
    AuditEventType,
    PlanningRunStatus,
    StepType,
    WorkflowType,
)
from app.domain.orchestration.models import (
    AgentStep,
    JsonObject,
    OrchestrationAuditEvent,
    PlanningRun,
    StepCheckpoint,
)
from app.orchestration.metrics import OrchestratorMetricsSnapshot


class PlanningRunCreateRequest(GenerateWeeklyPlanRequest):
    client_request_id: str = Field(min_length=1, max_length=120)


class PlanningRunCreateResponse(BaseModel):
    run_id: UUID
    status: PlanningRunStatus
    status_url: str


class PlanningRunResponse(BaseModel):
    id: UUID
    user_id: UUID
    workflow_type: WorkflowType
    status: PlanningRunStatus
    request_fingerprint: str
    result_reference: str | None
    current_step_id: UUID | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    version: int

    @classmethod
    def from_domain(cls, value: PlanningRun) -> Self:
        return cls(
            id=value.id,
            user_id=value.user_id,
            workflow_type=value.workflow_type,
            status=value.status,
            request_fingerprint=value.request_fingerprint,
            result_reference=value.result_reference,
            current_step_id=value.current_step_id,
            created_at=value.created_at,
            updated_at=value.updated_at,
            completed_at=value.completed_at,
            version=value.version,
        )


class AgentStepResponse(BaseModel):
    id: UUID
    run_id: UUID
    step_type: StepType
    status: AgentStepStatus
    sequence_no: int
    priority: int
    input_payload: JsonObject
    output_payload: JsonObject | None
    dependency_step_ids: tuple[UUID, ...]
    attempt_count: int
    max_attempts: int
    next_execute_at: datetime
    worker_id: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    version: int

    @classmethod
    def from_domain(cls, value: AgentStep) -> Self:
        return cls(
            id=value.id,
            run_id=value.run_id,
            step_type=value.step_type,
            status=value.status,
            sequence_no=value.sequence_no,
            priority=value.priority,
            input_payload=value.input_payload,
            output_payload=value.output_payload,
            dependency_step_ids=value.dependency_step_ids,
            attempt_count=value.attempt_count,
            max_attempts=value.max_attempts,
            next_execute_at=value.next_execute_at,
            worker_id=value.worker_id,
            lease_expires_at=value.lease_expires_at,
            heartbeat_at=value.heartbeat_at,
            last_error_code=value.last_error_code,
            last_error_message=value.last_error_message,
            created_at=value.created_at,
            updated_at=value.updated_at,
            completed_at=value.completed_at,
            version=value.version,
        )


class StepCheckpointResponse(BaseModel):
    id: UUID
    run_id: UUID
    step_id: UUID
    attempt_no: int
    step_type: StepType
    handler_version: str
    input_fingerprint: str
    output_payload: JsonObject
    result_reference: str | None
    run_status_after: PlanningRunStatus
    step_status_after: AgentStepStatus
    created_at: datetime

    @classmethod
    def from_domain(cls, value: StepCheckpoint) -> Self:
        return cls(**{field: getattr(value, field) for field in cls.model_fields})


class AuditEventResponse(BaseModel):
    id: UUID
    sequence_no: int
    run_id: UUID
    step_id: UUID | None
    event_type: AuditEventType
    from_status: str | None
    to_status: str | None
    worker_id: str | None
    attempt_no: int | None
    error_code: str | None
    metadata: JsonObject
    occurred_at: datetime

    @classmethod
    def from_domain(cls, value: OrchestrationAuditEvent) -> Self:
        return cls(**{field: getattr(value, field) for field in cls.model_fields})


class PlanningRunConfirmRequest(RequestModel):
    expected_plan_version: int = Field(ge=1)


class OrchestratorMetricsResponse(BaseModel):
    runs_created: int
    runs_completed: int
    runs_failed: int
    steps_claimed: int
    steps_succeeded: int
    step_attempts: int
    retries_scheduled: int
    leases_expired: int
    steps_reaped: int
    lease_conflicts: int
    waiting_user_count: int

    @classmethod
    def from_domain(cls, value: OrchestratorMetricsSnapshot) -> Self:
        return cls(**value.as_dict())
