"""The one owner of Tool retry, deadlines, bulkheads, circuits, and traces."""

from __future__ import annotations

import asyncio
from time import perf_counter

from pydantic import BaseModel, ValidationError

from app.domain.tools.enums import (
    CircuitState,
    ToolDegradationMode,
    ToolErrorCategory,
    ToolInvocationStatus,
)
from app.domain.tools.errors import ToolError
from app.domain.tools.models import (
    ToolInvocationContext,
    ToolInvocationResult,
    TypedToolResult,
)
from app.observability.context import (
    ObservabilityContext,
    current_observability_context,
)
from app.observability.facade import ObservabilityFacade
from app.reliability.clock import Clock
from app.reliability.fault_injection import FaultInjector
from app.reliability.sleeper import Sleeper
from app.tool_gateway.bulkhead import BulkheadManager
from app.tool_gateway.circuit_breaker import CircuitBreaker, CircuitKey
from app.tool_gateway.degradation import degradation_for
from app.tool_gateway.metrics import ToolMetrics
from app.tool_gateway.permissions import authorize
from app.tool_gateway.registry import ToolRegistry
from app.tool_gateway.retry import (
    RetryBudgetRegistry,
    backoff_ms,
    is_retryable,
    permitted_attempts,
)
from app.tool_gateway.traces import (
    ToolCallTrace,
    ToolInvocationSummary,
    ToolTraceStore,
)


class ToolGateway:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        clock: Clock,
        sleeper: Sleeper,
        circuits: CircuitBreaker | None = None,
        bulkheads: BulkheadManager | None = None,
        budgets: RetryBudgetRegistry | None = None,
        traces: ToolTraceStore | None = None,
        metrics: ToolMetrics | None = None,
        fault_injector: FaultInjector | None = None,
        observability: ObservabilityFacade | None = None,
    ) -> None:
        self.registry = registry
        self.clock = clock
        self.sleeper = sleeper
        self.circuits = circuits or CircuitBreaker(clock)
        self.bulkheads = bulkheads or BulkheadManager()
        self.budgets = budgets or RetryBudgetRegistry()
        self.traces = traces or ToolTraceStore()
        self.metrics = metrics or ToolMetrics()
        self.fault_injector = fault_injector
        self.observability = observability

    async def invoke(
        self, context: ToolInvocationContext, request: BaseModel
    ) -> TypedToolResult:
        """Export safe standard telemetry around the unchanged Phase 8A core."""

        if self.observability is None:
            return await self._invoke(context, request)
        inherited = current_observability_context()
        if inherited is not None:
            context = context.model_copy(
                update={
                    "correlation_id": inherited.correlation_id,
                    "request_id": inherited.request_id or context.request_id,
                    "run_id": inherited.run_id or context.run_id,
                    "step_id": inherited.step_id or context.step_id,
                }
            )
        registration = self.registry.get(context.tool_id.value, context.tool_version)
        descriptor = registration.descriptor
        safe_context = ObservabilityContext(
            correlation_id=context.correlation_id,
            request_id=context.request_id,
            run_id=context.run_id,
            step_id=context.step_id,
            operation_name="tool.invoke",
            component="tool_gateway",
        )
        started = perf_counter()
        with self.observability.operation(
            "tool.invocation",
            context=safe_context,
            attributes={
                "tool_id": context.tool_id.value,
                "tool_version": context.tool_version,
                "provider_name": descriptor.provider_name,
            },
        ) as span:
            result = await self._invoke(context, request)
            metadata = result.result
            outcome = metadata.status.value
            category = (
                metadata.error_category.value if metadata.error_category else "NONE"
            )
            degradation = metadata.degradation_mode.value
            labels = {
                "tool_id": context.tool_id.value,
                "tool_version": context.tool_version,
                "provider_name": descriptor.provider_name,
            }
            self.observability.record_counter(
                "fitweek_tool_invocations_total",
                labels={
                    **labels,
                    "outcome": outcome,
                    "error_category": category,
                    "degradation_mode": degradation,
                },
            )
            self.observability.record_histogram(
                "fitweek_tool_duration_seconds",
                (perf_counter() - started),
                labels={**labels, "outcome": outcome},
            )
            if metadata.status is ToolInvocationStatus.SUCCEEDED:
                span.succeed()
                self.observability.record_counter(
                    "fitweek_tool_successes_total", labels=labels
                )
            else:
                if metadata.error_category is ToolErrorCategory.BUSINESS_REJECTED:
                    span.reject(outcome, metadata.error_code)
                else:
                    span.fail(category, metadata.error_code)
                self.observability.record_counter(
                    "fitweek_tool_failures_total",
                    labels={**labels, "error_category": category},
                )
            if metadata.degradation_mode is not ToolDegradationMode.NONE:
                span.degrade(degradation)
                self.observability.record_counter(
                    "fitweek_tool_degraded_total",
                    labels={**labels, "degradation_mode": degradation},
                )
            self._record_standard_terminal_metrics(metadata, labels)
            self.observability.emit_log(
                event_name="tool_invocation_completed",
                context=safe_context,
                level=(
                    "INFO"
                    if metadata.status is ToolInvocationStatus.SUCCEEDED
                    else "WARNING"
                ),
                outcome=outcome,
                error_category=(category if category != "NONE" else None),
                error_code=metadata.error_code,
                degradation_mode=(
                    degradation
                    if metadata.degradation_mode is not ToolDegradationMode.NONE
                    else None
                ),
                duration_ms=metadata.latency_ms,
                span=span,
            )
            return result

    async def _invoke(
        self, context: ToolInvocationContext, request: BaseModel
    ) -> TypedToolResult:
        """Invoke one explicit adapter. No request payload is ever persisted here."""

        started = perf_counter()
        registration = self.registry.get(context.tool_id.value, context.tool_version)
        descriptor = registration.descriptor
        before = CircuitState.CLOSED
        try:
            authorize(context, descriptor)
        except ToolError as exc:
            return self._outcome(
                context,
                started,
                ToolInvocationStatus.REJECTED,
                before,
                before,
                error_category=exc.category,
                error_code=exc.code,
            )
        except ValueError:
            return self._outcome(
                context,
                started,
                ToolInvocationStatus.REJECTED,
                before,
                before,
                error_category=ToolErrorCategory.INVALID_REQUEST,
                error_code="TOOL_IDEMPOTENCY_KEY_REQUIRED",
            )
        if not isinstance(request, descriptor.request_model):
            return self._outcome(
                context,
                started,
                ToolInvocationStatus.REJECTED,
                before,
                before,
                error_category=ToolErrorCategory.INVALID_REQUEST,
                error_code="TOOL_REQUEST_TYPE_INVALID",
            )

        key = CircuitKey(
            context.tool_id.value,
            context.tool_version,
            descriptor.provider_name,
            descriptor.operation_class,
        )
        if descriptor.circuit_breaker_enabled:
            allowed, before = await self.circuits.allow(key)
            if not allowed:
                return self._outcome(
                    context,
                    started,
                    ToolInvocationStatus.DEGRADED,
                    before,
                    before,
                    degradation=degradation_for(context.tool_id),
                    error_category=ToolErrorCategory.CIRCUIT_OPEN,
                    error_code="TOOL_CIRCUIT_OPEN",
                )
        if self._remaining_ms(context) <= 0:
            return self._outcome(
                context,
                started,
                ToolInvocationStatus.REJECTED,
                before,
                before,
                error_category=ToolErrorCategory.DEADLINE_EXCEEDED,
                error_code="TOOL_DEADLINE_EXCEEDED",
            )

        semaphore = self.bulkheads.semaphore(
            (context.tool_id.value, context.tool_version), descriptor.bulkhead_limit
        )
        try:
            await asyncio.wait_for(
                semaphore.acquire(), self._remaining_seconds(context)
            )
        except TimeoutError:
            return self._outcome(
                context,
                started,
                ToolInvocationStatus.REJECTED,
                before,
                before,
                error_category=ToolErrorCategory.BULKHEAD_REJECTED,
                error_code="TOOL_BULKHEAD_REJECTED",
            )
        try:
            return await self._attempts(
                context, request, registration, key, before, started
            )
        finally:
            semaphore.release()

    def _record_standard_terminal_metrics(
        self,
        result: ToolInvocationResult,
        labels: dict[str, str],
    ) -> None:
        if self.observability is None or result.error_category is None:
            return
        metric_by_category = {
            ToolErrorCategory.TIMEOUT: "fitweek_tool_timeouts_total",
            ToolErrorCategory.RATE_LIMIT: "fitweek_tool_rate_limits_total",
            ToolErrorCategory.CIRCUIT_OPEN: "fitweek_tool_circuit_rejections_total",
            ToolErrorCategory.BULKHEAD_REJECTED: (
                "fitweek_tool_bulkhead_rejections_total"
            ),
            ToolErrorCategory.DEADLINE_EXCEEDED: (
                "fitweek_tool_deadline_exceeded_total"
            ),
        }
        metric = metric_by_category.get(result.error_category)
        if metric is not None:
            self.observability.record_counter(metric, labels=labels)
        if result.error_code == "TOOL_RETRY_BUDGET_EXHAUSTED":
            self.observability.record_counter(
                "fitweek_tool_retry_budget_exhausted_total", labels=labels
            )
        if result.circuit_state_after is CircuitState.OPEN:
            self.observability.record_counter(
                "fitweek_tool_circuit_open_total", labels=labels
            )

    async def _attempts(
        self,
        context: ToolInvocationContext,
        request: BaseModel,
        registration: object,
        key: CircuitKey,
        before: CircuitState,
        started: float,
    ) -> TypedToolResult:
        from app.tool_gateway.registry import ToolRegistration

        if not isinstance(registration, ToolRegistration):
            raise TypeError("Tool registry contained an invalid registration.")
        descriptor = registration.descriptor
        attempts = permitted_attempts(
            descriptor.side_effect_class, descriptor.max_attempts
        )
        budget = self.budgets.budget_for(context.correlation_id)
        last_category = ToolErrorCategory.INTERNAL_ERROR
        last_code = "TOOL_INTERNAL_ERROR"
        after = before
        completed_attempts = 0
        for attempt_no in range(1, attempts + 1):
            if not budget.consume():
                return self._outcome(
                    context,
                    started,
                    ToolInvocationStatus.REJECTED,
                    before,
                    after,
                    attempts=attempt_no - 1,
                    error_category=ToolErrorCategory.DEADLINE_EXCEEDED,
                    error_code="TOOL_RETRY_BUDGET_EXHAUSTED",
                )
            remaining_ms = self._remaining_ms(context)
            if remaining_ms <= 0:
                return self._outcome(
                    context,
                    started,
                    ToolInvocationStatus.REJECTED,
                    before,
                    after,
                    attempts=attempt_no - 1,
                    error_category=ToolErrorCategory.DEADLINE_EXCEEDED,
                    error_code="TOOL_DEADLINE_EXCEEDED",
                )
            attempt_started = perf_counter()
            completed_attempts = attempt_no
            attempt_timeout_ms = min(descriptor.default_timeout_ms, remaining_ms)
            try:
                injected = (
                    self.fault_injector.error_for(context.tool_id, attempt_no)
                    if self.fault_injector is not None
                    else None
                )
                if injected is not None:
                    raise ToolError(injected, f"TOOL_INJECTED_{injected.value}")
                response = await asyncio.wait_for(
                    registration.adapter.invoke_once(context, request),
                    timeout=attempt_timeout_ms / 1000,
                )
                if not isinstance(response, descriptor.response_model):
                    raise ToolError(
                        ToolErrorCategory.RESPONSE_INVALID, "TOOL_RESPONSE_TYPE_INVALID"
                    )
                response = descriptor.response_model.model_validate(response)
                after = (
                    await self.circuits.record_success(key)
                    if descriptor.circuit_breaker_enabled
                    else before
                )
                self._trace(
                    context,
                    descriptor,
                    attempt_no,
                    "SUCCEEDED",
                    None,
                    None,
                    attempt_started,
                    before,
                    after,
                    ToolDegradationMode.NONE,
                )
                return self._outcome(
                    context,
                    started,
                    ToolInvocationStatus.SUCCEEDED,
                    before,
                    after,
                    attempts=attempt_no,
                    response=response,
                )
            except TimeoutError:
                if attempt_timeout_ms >= remaining_ms:
                    last_category, last_code = (
                        ToolErrorCategory.DEADLINE_EXCEEDED,
                        "TOOL_DEADLINE_EXCEEDED",
                    )
                else:
                    last_category, last_code = (
                        ToolErrorCategory.TIMEOUT,
                        "TOOL_ATTEMPT_TIMEOUT",
                    )
            except ToolError as exc:
                last_category, last_code = exc.category, exc.code
            except ValidationError:
                last_category, last_code = (
                    ToolErrorCategory.RESPONSE_INVALID,
                    "TOOL_RESPONSE_INVALID",
                )
            except Exception:
                last_category, last_code = (
                    ToolErrorCategory.INTERNAL_ERROR,
                    "TOOL_ADAPTER_INTERNAL_ERROR",
                )
            self._trace(
                context,
                descriptor,
                attempt_no,
                "FAILED",
                last_category,
                last_code,
                attempt_started,
                before,
                before,
                ToolDegradationMode.NONE,
            )
            if not is_retryable(last_category) or attempt_no >= attempts:
                break
            delay = backoff_ms(attempt_no + 1)
            if delay >= self._remaining_ms(context):
                last_category, last_code = (
                    ToolErrorCategory.DEADLINE_EXCEEDED,
                    "TOOL_DEADLINE_EXCEEDED",
                )
                break
            await self.sleeper.sleep_ms(delay)
        status = (
            ToolInvocationStatus.FAILED_RETRYABLE
            if is_retryable(last_category)
            else ToolInvocationStatus.FAILED_PERMANENT
        )
        after = (
            await self.circuits.record_failure(key, last_category)
            if descriptor.circuit_breaker_enabled
            else before
        )
        return self._outcome(
            context,
            started,
            status,
            before,
            after,
            attempts=completed_attempts,
            degradation=(
                degradation_for(context.tool_id)
                if descriptor.supports_degradation
                else ToolDegradationMode.NONE
            ),
            error_category=last_category,
            error_code=last_code,
        )

    def _remaining_ms(self, context: ToolInvocationContext) -> int:
        return max(
            0, int((context.deadline_at - self.clock.now()).total_seconds() * 1000)
        )

    def _remaining_seconds(self, context: ToolInvocationContext) -> float:
        return max(0.001, self._remaining_ms(context) / 1000)

    def _outcome(
        self,
        context: ToolInvocationContext,
        started: float,
        status: ToolInvocationStatus,
        before: CircuitState,
        after: CircuitState,
        *,
        attempts: int = 0,
        response: BaseModel | None = None,
        degradation: ToolDegradationMode = ToolDegradationMode.NONE,
        error_category: ToolErrorCategory | None = None,
        error_code: str | None = None,
    ) -> TypedToolResult:
        latency = (perf_counter() - started) * 1000
        self.metrics.record_invocation(
            tool_id=context.tool_id,
            status=status,
            error_category=error_category,
            error_code=error_code,
            degradation=degradation,
            circuit_before=before,
            circuit_after=after,
            latency_ms=latency,
        )
        self.traces.add_summary(
            ToolInvocationSummary(
                invocation_id=context.invocation_id,
                correlation_id=context.correlation_id,
                user_id=context.user_id,
                caller=context.caller.value,
                tool_id=context.tool_id,
                tool_version=context.tool_version,
                attempt_count=attempts,
                status=status,
                error_category=error_category,
                error_code=error_code,
                latency_ms=latency,
                circuit_before=before,
                circuit_after=after,
                degradation_mode=degradation,
                created_at=self.clock.now(),
                run_id=context.run_id,
                step_id=context.step_id,
            )
        )
        return TypedToolResult(
            result=ToolInvocationResult(
                invocation_id=context.invocation_id,
                tool_id=context.tool_id,
                tool_version=context.tool_version,
                status=status,
                degradation_mode=degradation,
                attempt_count=attempts,
                latency_ms=latency,
                response_reference=None,
                error_category=error_category,
                error_code=error_code,
                circuit_state_before=before,
                circuit_state_after=after,
                created_at=self.clock.now(),
            ),
            response=response,
        )

    def _trace(
        self,
        context: ToolInvocationContext,
        descriptor: object,
        attempt_no: int,
        status: str,
        category: ToolErrorCategory | None,
        code: str | None,
        started: float,
        before: CircuitState,
        after: CircuitState,
        degradation: ToolDegradationMode,
    ) -> None:
        from app.domain.tools.models import ToolDescriptor

        if not isinstance(descriptor, ToolDescriptor):
            return
        latency_ms = (perf_counter() - started) * 1000
        self.metrics.record_attempt(
            tool_id=context.tool_id,
            status=status,
            error_category=category,
            latency_ms=latency_ms,
            attempt_no=attempt_no,
        )
        self.traces.add(
            ToolCallTrace(
                invocation_id=context.invocation_id,
                correlation_id=context.correlation_id,
                user_id=context.user_id,
                caller=context.caller.value,
                tool_id=context.tool_id,
                tool_version=context.tool_version,
                provider_name=descriptor.provider_name,
                provider_version=descriptor.version,
                attempt_no=attempt_no,
                status=status,
                error_category=category,
                error_code=code,
                latency_ms=latency_ms,
                circuit_before=before,
                circuit_after=after,
                degradation_mode=degradation,
                created_at=self.clock.now(),
                run_id=context.run_id,
                step_id=context.step_id,
            )
        )
        if self.observability is not None:
            labels = {
                "tool_id": context.tool_id.value,
                "tool_version": context.tool_version,
                "provider_name": descriptor.provider_name,
            }
            category_value = category.value if category is not None else "NONE"
            self.observability.record_counter(
                "fitweek_tool_attempts_total",
                labels={
                    **labels,
                    "outcome": status,
                    "error_category": category_value,
                },
            )
            if attempt_no > 1:
                self.observability.record_counter(
                    "fitweek_tool_retries_total",
                    labels={**labels, "outcome": status},
                )
            with self.observability.start_span(
                "tool.attempt",
                context=ObservabilityContext(
                    correlation_id=context.correlation_id,
                    request_id=context.request_id,
                    run_id=context.run_id,
                    step_id=context.step_id,
                    operation_name="tool.attempt",
                    component="tool_gateway",
                ),
                attributes={
                    "tool_id": context.tool_id.value,
                    "tool_version": context.tool_version,
                    "provider_name": descriptor.provider_name,
                    "attempt_number": attempt_no,
                    "outcome": status,
                    "error_category": category_value,
                    "circuit_state": after.value,
                },
            ) as span:
                if status == "SUCCEEDED":
                    span.succeed()
                elif category is ToolErrorCategory.BUSINESS_REJECTED:
                    span.reject(status, code)
                else:
                    span.fail(category_value, code)
