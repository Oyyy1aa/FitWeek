"""Immutable Run, Step, Checkpoint, and Audit domain records."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from app.domain.common import (
    DomainValidationError,
    require_non_blank,
    require_utc_datetime,
    require_version,
)
from app.domain.orchestration.enums import (
    AgentStepStatus,
    AuditEventType,
    PlanningRunStatus,
    StepType,
    WorkflowType,
)

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_FORBIDDEN_KEY_PARTS = ("password", "secret", "token", "database_url", "redis_url")
_FORBIDDEN_VALUE_PARTS = (
    "mysql+asyncmy://",
    "postgresql+asyncpg://",
    "redis://",
    "rediss://",
    "begin private key",
)


def safe_json_object(value: dict[str, object] | JsonObject) -> JsonObject:
    """Return a detached JSON object and reject likely secret-bearing payloads."""

    def scan(item: object, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise DomainValidationError(f"{path} keys must be strings.")
                normalized = key.casefold()
                if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                    raise DomainValidationError(f"{path} contains a forbidden key.")
                scan(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                scan(child, f"{path}[{index}]")
        elif isinstance(item, str):
            normalized = item.casefold()
            if any(part in normalized for part in _FORBIDDEN_VALUE_PARTS):
                raise DomainValidationError(f"{path} contains a forbidden value.")

    scan(value, "payload")
    try:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise DomainValidationError("payload must be JSON serializable.") from exc
    if not isinstance(decoded, dict):
        raise DomainValidationError("payload must be a JSON object.")
    return cast(JsonObject, decoded)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanningRun:
    id: UUID
    user_id: UUID
    workflow_type: WorkflowType
    status: PlanningRunStatus
    request_fingerprint: str
    input_payload: JsonObject
    result_reference: str | None
    current_step_id: UUID | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    version: int
    client_request_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.user_id, UUID):
            raise DomainValidationError("run id and user_id must be UUID values.")
        if not isinstance(self.workflow_type, WorkflowType):
            raise DomainValidationError("workflow_type is invalid.")
        if not isinstance(self.status, PlanningRunStatus):
            raise DomainValidationError("run status is invalid.")
        require_non_blank(self.request_fingerprint, "request_fingerprint")
        require_non_blank(self.client_request_id, "client_request_id")
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.updated_at, "updated_at")
        if self.completed_at is not None:
            require_utc_datetime(self.completed_at, "completed_at")
        require_version(self.version)
        object.__setattr__(self, "input_payload", safe_json_object(self.input_payload))


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentStep:
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
    lease_token: UUID | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    version: int
    fencing_token: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.run_id, UUID):
            raise DomainValidationError("step id and run_id must be UUID values.")
        if not isinstance(self.step_type, StepType):
            raise DomainValidationError("step_type is invalid.")
        if not isinstance(self.status, AgentStepStatus):
            raise DomainValidationError("step status is invalid.")
        if self.sequence_no < 1 or self.max_attempts < 1 or self.attempt_count < 0:
            raise DomainValidationError("step counters are invalid.")
        if self.attempt_count > self.max_attempts:
            raise DomainValidationError("attempt_count exceeds max_attempts.")
        if self.fencing_token < 0:
            raise DomainValidationError("fencing_token must not be negative.")
        if len(set(self.dependency_step_ids)) != len(self.dependency_step_ids):
            raise DomainValidationError("dependency_step_ids must be unique.")
        require_utc_datetime(self.next_execute_at, "next_execute_at")
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.updated_at, "updated_at")
        for name, timestamp in (
            ("lease_expires_at", self.lease_expires_at),
            ("heartbeat_at", self.heartbeat_at),
            ("completed_at", self.completed_at),
        ):
            if timestamp is not None:
                require_utc_datetime(timestamp, name)
        require_version(self.version)
        object.__setattr__(self, "input_payload", safe_json_object(self.input_payload))
        if self.output_payload is not None:
            object.__setattr__(
                self,
                "output_payload",
                safe_json_object(self.output_payload),
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimedStep:
    run: PlanningRun
    step: AgentStep

    @property
    def lease_token(self) -> UUID:
        if self.step.lease_token is None:
            raise DomainValidationError("claimed step is missing a lease token.")
        return self.step.lease_token

    @property
    def fencing_token(self) -> int:
        """Monotonic ownership generation persisted with the claimed step."""

        return self.step.fencing_token


@dataclass(frozen=True, slots=True, kw_only=True)
class StepCheckpoint:
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

    def __post_init__(self) -> None:
        if self.attempt_no < 1:
            raise DomainValidationError("checkpoint attempt_no must be positive.")
        require_non_blank(self.handler_version, "handler_version")
        require_non_blank(self.input_fingerprint, "input_fingerprint")
        require_utc_datetime(self.created_at, "created_at")
        object.__setattr__(
            self, "output_payload", safe_json_object(self.output_payload)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OrchestrationAuditEvent:
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

    def __post_init__(self) -> None:
        if self.sequence_no < 1:
            raise DomainValidationError("audit sequence_no must be positive.")
        require_utc_datetime(self.occurred_at, "occurred_at")
        object.__setattr__(self, "metadata", safe_json_object(self.metadata))


@dataclass(frozen=True, slots=True, kw_only=True)
class RunCreationResult:
    run: PlanningRun
    created: bool


def detached[T](value: T) -> T:
    """Protect repository state from mutable payload aliases."""

    return deepcopy(value)
