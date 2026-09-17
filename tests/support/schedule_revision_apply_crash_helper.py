"""Controlled Schedule Build crash boundary for recovery integration tests."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta

from app.config import get_settings
from app.domain.orchestration.enums import StepType
from app.orchestration.clock import SystemClock
from app.orchestration.handler import StepExecutionContext
from app.orchestration.mysql_runtime import build_mysql_cli_runtime


async def apply_then_exit(worker_id: str) -> int:
    """Commit Schedule Build state then exit before generic completion."""

    settings = get_settings()
    runtime, database = await build_mysql_cli_runtime(settings, worker_id=worker_id)
    try:
        claim = await runtime.repository.claim_next_step(
            worker_id=worker_id,
            lease_duration=timedelta(seconds=settings.orchestrator_lease_seconds),
            now=SystemClock().now(),
        )
        if (
            claim is None
            or claim.step.step_type is not StepType.BUILD_SCHEDULE_PLAN_REVISION
        ):
            return 1
        handler = runtime.registry.get(claim.step.step_type)
        result = await handler.execute(StepExecutionContext(claim=claim))
        print(
            json.dumps(
                {
                    "run_id": str(claim.run.id),
                    "step_id": str(claim.step.id),
                    "fencing_token": claim.fencing_token,
                    "application_result_id": result.result_reference,
                }
            )
        )
        return 0
    finally:
        if runtime.schedule_calendar_gateway is not None:
            await runtime.schedule_calendar_gateway.close()
        if runtime.schedule_model_gateway is not None:
            await runtime.schedule_model_gateway.close()
        if runtime.session_design_model_gateway is not None:
            await runtime.session_design_model_gateway.close()
        if runtime.profile_agent_model_gateway is not None:
            await runtime.profile_agent_model_gateway.close()
        await database.dispose()


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(
            apply_then_exit(
                sys.argv[1] if len(sys.argv) > 1 else "schedule-build-crash"
            )
        )
    )
