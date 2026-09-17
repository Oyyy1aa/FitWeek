"""One cooperative worker that executes exactly one claimed step at a time."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter
from uuid import UUID

from app.domain.orchestration.enums import StepOutcome
from app.domain.orchestration.errors import StepLeaseLost
from app.domain.orchestration.models import ClaimedStep
from app.domain.orchestration.repositories import OrchestrationRepository
from app.observability.context import (
    ObservabilityContext,
    reset_observability_context,
    set_observability_context,
)
from app.observability.facade import ObservabilityFacade
from app.orchestration.clock import Clock
from app.orchestration.handler import (
    HandlerContractError,
    PermanentStepError,
    RetryableStepError,
    StepExecutionContext,
    StepExecutionResult,
    StepHandler,
)
from app.orchestration.handler_registry import HandlerNotRegistered, HandlerRegistry
from app.orchestration.retry_policy import RetryPolicy
from app.orchestration.workflow import DeterministicGenerationWorkflow


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerIterationResult:
    claimed: bool
    step_id: UUID | None
    outcome: str


class OrchestrationWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        repository: OrchestrationRepository,
        registry: HandlerRegistry,
        clock: Clock,
        retry_policy: RetryPolicy,
        lease_duration: timedelta,
        handler_timeout_seconds: float,
        poll_interval_seconds: float,
        observability: ObservabilityFacade | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank.")
        if lease_duration.total_seconds() <= 0 or handler_timeout_seconds <= 0:
            raise ValueError("lease and handler timeout must be positive.")
        if poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive.")
        self.worker_id = worker_id
        self._repository = repository
        self._registry = registry
        self._clock = clock
        self._retry_policy = retry_policy
        self._lease_duration = lease_duration
        self._handler_timeout_seconds = handler_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._observability = observability

    async def run_once(self) -> WorkerIterationResult:
        claim = await self._repository.claim_next_step(
            worker_id=self.worker_id,
            lease_duration=self._lease_duration,
            now=self._clock.now(),
        )
        if claim is None:
            return WorkerIterationResult(claimed=False, step_id=None, outcome="EMPTY")
        if self._observability is not None:
            context = ObservabilityContext(
                correlation_id=claim.run.id,
                request_id=claim.run.client_request_id,
                run_id=claim.run.id,
                step_id=claim.step.id,
                operation_name="orchestrator.run",
                component="orchestrator",
            )
            with self._observability.operation(
                "orchestrator.run",
                context=context,
                attributes={"workflow_type": claim.run.workflow_type.value},
            ) as run_span:
                with self._observability.operation(
                    "orchestrator.step",
                    context=context.model_copy(
                        update={"operation_name": "orchestrator.step"}
                    ),
                    attributes={
                        "workflow_type": claim.run.workflow_type.value,
                        "step_type": claim.step.step_type.value,
                    },
                ) as step_span:
                    started = perf_counter()
                    result = await self._process_claim(claim)
                    if result.outcome in {"SUCCEEDED", "WAITING_USER"}:
                        step_span.succeed(result.outcome)
                        run_span.succeed(result.outcome)
                    else:
                        step_span.fail("STEP_FAILURE", result.outcome)
                        run_span.fail("STEP_FAILURE", result.outcome)
                    self._observability.emit_log(
                        event_name="orchestrator_step_completed",
                        context=context,
                        level=(
                            "INFO"
                            if result.outcome in {"SUCCEEDED", "WAITING_USER"}
                            else "WARNING"
                        ),
                        outcome=result.outcome,
                        error_category=(
                            None
                            if result.outcome in {"SUCCEEDED", "WAITING_USER"}
                            else "STEP_FAILURE"
                        ),
                        span=step_span,
                    )
                    step_labels = {
                        "step_type": claim.step.step_type.value,
                        "outcome": result.outcome,
                    }
                    self._observability.record_counter(
                        "fitweek_orchestrator_steps_total", labels=step_labels
                    )
                    self._observability.record_histogram(
                        "fitweek_orchestrator_step_duration_seconds",
                        perf_counter() - started,
                        labels=step_labels,
                    )
                    if claim.step.attempt_count > 1:
                        self._observability.record_counter(
                            "fitweek_orchestrator_step_retries_total",
                            labels=step_labels,
                        )
                    if claim.step.sequence_no == 1:
                        self._observability.record_counter(
                            "fitweek_orchestrator_runs_total",
                            labels={
                                "workflow_type": claim.run.workflow_type.value,
                                "outcome": "STARTED",
                            },
                        )
                    if (
                        result.outcome == "SUCCEEDED"
                        and claim.step.step_type.value.startswith("FINALIZE_")
                    ):
                        self._observability.record_counter(
                            "fitweek_orchestrator_runs_total",
                            labels={
                                "workflow_type": claim.run.workflow_type.value,
                                "outcome": "COMPLETED",
                            },
                        )
                    return result
        return await self._process_claim(claim)

    async def _process_claim(self, claim: ClaimedStep) -> WorkerIterationResult:
        token = None
        if self._observability is not None:
            token = set_observability_context(
                ObservabilityContext(
                    correlation_id=claim.run.id,
                    request_id=claim.run.client_request_id,
                    run_id=claim.run.id,
                    step_id=claim.step.id,
                    operation_name="orchestrator.step",
                    component="orchestrator",
                )
            )
        try:
            handler = self._registry.get(claim.step.step_type)
            async with asyncio.timeout(self._handler_timeout_seconds):
                result = await self._execute_with_heartbeat(handler, claim)
            if not isinstance(result, StepExecutionResult):
                raise HandlerContractError("Handler must return a StepExecutionResult.")
            progression = DeterministicGenerationWorkflow.progression(
                claim.step.step_type,
                result.outcome,
            )
            now = self._clock.now()
            if result.outcome is StepOutcome.WAITING_USER:
                await self._repository.mark_waiting_user(
                    claim=claim,
                    output_payload=result.output_payload,
                    run_status_after=progression.run_status_after,
                    now=now,
                )
                return WorkerIterationResult(
                    claimed=True,
                    step_id=claim.step.id,
                    outcome="WAITING_USER",
                )
            await self._repository.complete_step(
                claim=claim,
                handler_version=handler.version,
                output_payload=result.output_payload,
                result_reference=result.result_reference,
                next_step_type=progression.next_step_type,
                run_status_after=progression.run_status_after,
                now=now,
            )
            return WorkerIterationResult(
                claimed=True,
                step_id=claim.step.id,
                outcome="SUCCEEDED",
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return await self._record_failure(
                claim=claim,
                code="STEP_HANDLER_TIMEOUT",
                message="Handler exceeded its bounded execution timeout.",
                retryable=True,
            )
        except HandlerNotRegistered:
            return await self._record_failure(
                claim=claim,
                code="HANDLER_NOT_REGISTERED",
                message="No handler is registered for the claimed step type.",
                retryable=False,
            )
        except RetryableStepError as exc:
            return await self._record_failure(
                claim=claim,
                code=exc.code,
                message=str(exc),
                retryable=True,
            )
        except PermanentStepError as exc:
            return await self._record_failure(
                claim=claim,
                code=exc.code,
                message=str(exc),
                retryable=False,
            )
        except (TypeError, ValueError) as exc:
            return await self._record_failure(
                claim=claim,
                code="HANDLER_CONTRACT_VIOLATION",
                message=str(exc),
                retryable=False,
            )
        except Exception:
            return await self._record_failure(
                claim=claim,
                code="HANDLER_UNEXPECTED_ERROR",
                message="Handler raised an unexpected internal error.",
                retryable=False,
            )
        finally:
            if token is not None:
                reset_observability_context(token)

    async def _execute_with_heartbeat(
        self,
        handler: StepHandler,
        claim: ClaimedStep,
    ) -> StepExecutionResult:
        """Run the Handler outside the lock while periodically renewing ownership."""

        task = asyncio.create_task(handler.execute(StepExecutionContext(claim=claim)))
        interval = max(0.01, self._lease_duration.total_seconds() / 3)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    return task.result()
                await self._repository.heartbeat(
                    step_id=claim.step.id,
                    worker_id=self.worker_id,
                    lease_token=claim.lease_token,
                    fencing_token=claim.fencing_token,
                    lease_duration=self._lease_duration,
                    now=self._clock.now(),
                )
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _record_failure(
        self,
        *,
        claim: ClaimedStep,
        code: str,
        message: str,
        retryable: bool,
    ) -> WorkerIterationResult:
        now = self._clock.now()
        retry_at = (
            self._retry_policy.retry_at(
                attempt_no=claim.step.attempt_count,
                now=now,
            )
            if retryable
            else None
        )
        try:
            failed = await self._repository.fail_step(
                claim=claim,
                error_code=code,
                error_message=message,
                retry_at=retry_at,
                now=now,
            )
        except StepLeaseLost:
            return WorkerIterationResult(
                claimed=True,
                step_id=claim.step.id,
                outcome="LEASE_LOST",
            )
        return WorkerIterationResult(
            claimed=True,
            step_id=claim.step.id,
            outcome=failed.status.value,
        )

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            result = await self.run_once()
            if result.claimed:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                continue
