"""Small immutable orchestration fixtures."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.orchestration.enums import (
    AgentStepStatus,
    PlanningRunStatus,
    StepType,
    WorkflowType,
)
from app.domain.orchestration.models import AgentStep, PlanningRun

NOW = datetime(2030, 1, 1, 8, tzinfo=UTC)
USER_ID = UUID("00000000-0000-4000-8000-000000000001")


def make_run(
    *,
    run_id: UUID | None = None,
    user_id: UUID = USER_ID,
    client_request_id: str = "request-1",
    fingerprint: str = "f" * 64,
) -> PlanningRun:
    return PlanningRun(
        id=run_id or uuid4(),
        user_id=user_id,
        workflow_type=WorkflowType.DETERMINISTIC_PLAN_GENERATION,
        status=PlanningRunStatus.CREATED,
        request_fingerprint=fingerprint,
        input_payload={},
        result_reference=None,
        current_step_id=None,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
        version=1,
        client_request_id=client_request_id,
    )


def make_step(
    run_id: UUID,
    *,
    step_id: UUID | None = None,
    step_type: StepType = StepType.LOAD_PROFILE_CONTEXT,
    status: AgentStepStatus = AgentStepStatus.READY,
    sequence_no: int = 1,
    priority: int = 100,
    dependencies: tuple[UUID, ...] = (),
    max_attempts: int = 3,
) -> AgentStep:
    return AgentStep(
        id=step_id or uuid4(),
        run_id=run_id,
        step_type=step_type,
        status=status,
        sequence_no=sequence_no,
        priority=priority,
        input_payload={},
        output_payload=None,
        dependency_step_ids=dependencies,
        attempt_count=0,
        max_attempts=max_attempts,
        next_execute_at=NOW,
        worker_id=None,
        lease_token=None,
        lease_expires_at=None,
        heartbeat_at=None,
        last_error_code=None,
        last_error_message=None,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
        version=1,
    )
