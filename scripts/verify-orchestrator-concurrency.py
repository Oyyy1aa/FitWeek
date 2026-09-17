"""Verify 4 coroutine claimers against 100 independent in-memory READY steps."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import UUID, uuid4

from app.domain.orchestration.enums import (
    AgentStepStatus,
    PlanningRunStatus,
    StepType,
    WorkflowType,
)
from app.domain.orchestration.models import AgentStep, PlanningRun
from app.persistence.memory.orchestration_repository import (
    InMemoryOrchestrationRepository,
)


async def verify() -> dict[str, int | float]:
    now = datetime.now(UTC)
    run_id = uuid4()
    run = PlanningRun(
        id=run_id,
        user_id=UUID("00000000-0000-4000-8000-000000000001"),
        workflow_type=WorkflowType.DETERMINISTIC_PLAN_GENERATION,
        status=PlanningRunStatus.CREATED,
        request_fingerprint="c" * 64,
        input_payload={},
        result_reference=None,
        current_step_id=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
        version=1,
        client_request_id="concurrency-verification",
    )
    steps = tuple(
        AgentStep(
            id=uuid4(),
            run_id=run_id,
            step_type=StepType.LOAD_PROFILE_CONTEXT,
            status=AgentStepStatus.READY,
            sequence_no=index + 1,
            priority=100,
            input_payload={"index": index},
            output_payload=None,
            dependency_step_ids=(),
            attempt_count=0,
            max_attempts=3,
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
        for index in range(100)
    )
    repository = InMemoryOrchestrationRepository()
    await repository.create_run_with_initial_steps(run, steps)
    claimed: list[UUID] = []
    successful: list[UUID] = []
    failures = 0

    async def worker(worker_id: str) -> None:
        nonlocal failures
        while True:
            claim = await repository.claim_next_step(
                worker_id=worker_id,
                lease_duration=timedelta(seconds=10),
                now=now,
            )
            if claim is None:
                return
            claimed.append(claim.step.id)
            try:
                completed = await repository.complete_step(
                    claim=claim,
                    handler_version="concurrency-verification-v1",
                    output_payload={"ok": True},
                    result_reference=None,
                    next_step_type=None,
                    run_status_after=PlanningRunStatus.COLLECTING_PROFILE,
                    now=now,
                )
            except Exception:
                failures += 1
            else:
                successful.append(completed.id)

    started = perf_counter()
    await asyncio.gather(*(worker(f"worker-{index + 1}") for index in range(4)))
    elapsed_ms = (perf_counter() - started) * 1000
    total_attempts = sum(
        item.attempt_count for item in await repository.list_steps(run_id)
    )
    return {
        "total_steps": len(steps),
        "unique_claimed_steps": len(set(claimed)),
        "duplicate_successful_claims": len(successful) - len(set(successful)),
        "successful_steps": len(successful),
        "failed_steps": failures,
        "total_attempts": total_attempts,
        "elapsed_ms": round(elapsed_ms, 3),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), sort_keys=True))
