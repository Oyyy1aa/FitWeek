"""Bounded primary/retry/backup/template orchestration with safe traces."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TypeVar
from uuid import UUID, uuid4

from app.domain.common import utc_now
from app.domain.model_gateway.enums import (
    FallbackType,
    ModelAttemptOutcome,
    ModelErrorCode,
    ProviderRole,
)
from app.domain.model_gateway.errors import (
    ModelGatewayError,
    ModelProviderUnavailableError,
)
from app.domain.model_gateway.models import (
    ModelCallTrace,
    ModelGatewayResult,
    ModelProviderResponse,
    ModelRequest,
    ModelTraceContext,
)
from app.domain.model_gateway.protocols import ModelProvider
from app.model_gateway.limiter import ProcessLocalModelLimiter
from app.model_gateway.metrics import ModelGatewayMetrics, ModelGatewayMetricsSnapshot
from app.model_gateway.redaction import redact_text
from app.model_gateway.retry_policy import AsyncioSleeper, ModelRetryPolicy, Sleeper
from app.observability.context import (
    ObservabilityContext,
    current_observability_context,
)
from app.observability.facade import ObservabilityFacade

T = TypeVar("T")


class InMemoryModelTraceStore:
    """Append-only process-local trace store with user-scoped reads."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._traces: list[ModelCallTrace] = []

    async def append(self, trace: ModelCallTrace) -> None:
        async with self._lock:
            self._traces.append(trace)

    async def list_for_request(
        self,
        *,
        user_id: UUID,
        request_id: UUID,
    ) -> tuple[ModelCallTrace, ...]:
        async with self._lock:
            return tuple(
                trace
                for trace in self._traces
                if trace.user_id == user_id and trace.request_id == request_id
            )


class ModelGateway:
    """Invoke at most two primary attempts and one backup attempt."""

    def __init__(
        self,
        *,
        primary: ModelProvider,
        primary_model: str,
        backup: ModelProvider | None,
        backup_model: str,
        retry_policy: ModelRetryPolicy,
        limiter: ProcessLocalModelLimiter,
        metrics: ModelGatewayMetrics | None = None,
        traces: InMemoryModelTraceStore | None = None,
        sleeper: Sleeper | None = None,
        observability: ObservabilityFacade | None = None,
    ) -> None:
        self._primary = primary
        self._primary_model = primary_model
        self._backup = backup
        self._backup_model = backup_model
        self._retry_policy = retry_policy
        self._limiter = limiter
        self._metrics = metrics or ModelGatewayMetrics()
        self._traces = traces or InMemoryModelTraceStore()
        self._sleeper = sleeper or AsyncioSleeper()
        self._observability = observability

    async def invoke(
        self,
        *,
        request: ModelRequest,
        trace_context: ModelTraceContext,
        validator: Callable[[str], T],
        template_factory: Callable[[], T],
    ) -> ModelGatewayResult[T]:
        """Trace one Agent/Model invocation without exposing prompt content."""

        if self._observability is None:
            return await self._invoke_core(
                request=request,
                trace_context=trace_context,
                validator=validator,
                template_factory=template_factory,
            )
        inherited = current_observability_context()
        safe_context = ObservabilityContext(
            correlation_id=(
                inherited.correlation_id
                if inherited is not None
                else request.request_id
            ),
            request_id=str(request.request_id),
            run_id=inherited.run_id if inherited is not None else None,
            step_id=inherited.step_id if inherited is not None else None,
            operation_name="model.gateway.invoke",
            component="model_gateway",
        )
        started = time.perf_counter()
        with self._observability.operation(
            "model.gateway.invocation",
            context=safe_context,
            attributes={"provider_name": self._primary.provider_name},
        ) as model_span:
            try:
                result = await self._invoke_core(
                    request=request,
                    trace_context=trace_context,
                    validator=validator,
                    template_factory=template_factory,
                )
            except Exception:
                labels = {
                    "provider_name": self._primary.provider_name,
                    "outcome": "FAILED",
                }
                self._observability.record_counter(
                    "fitweek_model_invocations_total", labels=labels
                )
                self._observability.record_histogram(
                    "fitweek_model_duration_seconds",
                    time.perf_counter() - started,
                    labels=labels,
                )
                model_span.fail("MODEL_FAILURE", "MODEL_GATEWAY_FAILED")
                raise
            outcome = "FALLBACK" if result.fallback_used else "SUCCEEDED"
            labels = {
                "provider_name": result.provider_name,
                "outcome": outcome,
            }
            self._observability.record_counter(
                "fitweek_model_invocations_total", labels=labels
            )
            self._observability.record_histogram(
                "fitweek_model_duration_seconds",
                time.perf_counter() - started,
                labels=labels,
            )
            if result.fallback_used:
                self._observability.record_counter(
                    "fitweek_model_fallback_total", labels=labels
                )
                self._observability.record_counter(
                    "fitweek_agent_fallbacks_total",
                    labels={
                        "agent_type": trace_context.agent_name,
                        "outcome": outcome,
                    },
                )
                model_span.degrade("DETERMINISTIC_FALLBACK")
            model_span.succeed(outcome)
            return result

    async def _invoke_core(
        self,
        *,
        request: ModelRequest,
        trace_context: ModelTraceContext,
        validator: Callable[[str], T],
        template_factory: Callable[[], T],
    ) -> ModelGatewayResult[T]:
        """Return validated output or an explicitly marked safe template."""

        self._metrics.request_started()
        attempts = 0
        try:
            try:
                async with self._limiter.slot():
                    for primary_attempt in range(
                        1, self._retry_policy.primary_attempts + 1
                    ):
                        if attempts >= self._retry_policy.max_attempts:
                            break
                        attempts += 1
                        outcome = await self._attempt(
                            provider=self._primary,
                            role=ProviderRole.PRIMARY,
                            request=replace(request, model=self._primary_model),
                            trace_context=trace_context,
                            attempt_no=attempts,
                            validator=validator,
                        )
                        if isinstance(outcome, ModelGatewayResult):
                            return outcome
                        if not outcome.retryable:
                            break
                        if primary_attempt < self._retry_policy.primary_attempts:
                            await self._sleeper.sleep(
                                self._retry_policy.retry_delay_seconds
                            )

                    if (
                        self._backup is not None
                        and attempts < self._retry_policy.max_attempts
                        and self._retry_policy.backup_attempts > 0
                    ):
                        attempts += 1
                        backup_outcome = await self._attempt(
                            provider=self._backup,
                            role=ProviderRole.BACKUP,
                            request=replace(request, model=self._backup_model),
                            trace_context=trace_context,
                            attempt_no=attempts,
                            validator=validator,
                        )
                        if isinstance(backup_outcome, ModelGatewayResult):
                            return backup_outcome
            except ModelGatewayError as exc:
                self._metrics.error(exc.code)
            return await self._template_result(
                request=request,
                trace_context=trace_context,
                attempt_no=attempts + 1,
                provider_attempts=attempts,
                template_factory=template_factory,
            )
        finally:
            self._metrics.request_finished()

    async def _attempt(
        self,
        *,
        provider: ModelProvider,
        role: ProviderRole,
        request: ModelRequest,
        trace_context: ModelTraceContext,
        attempt_no: int,
        validator: Callable[[str], T],
    ) -> ModelGatewayResult[T] | ModelGatewayError:
        self._metrics.provider_attempted()
        started = time.perf_counter()
        response: ModelProviderResponse | None = None
        try:
            response = await provider.invoke(request)
            value = validator(response.raw_text)
        except ModelGatewayError as exc:
            self._metrics.error(exc.code)
            await self._record_trace(
                request=request,
                context=trace_context,
                provider=provider,
                role=role,
                attempt_no=attempt_no,
                outcome=ModelAttemptOutcome.ERROR,
                error=exc,
                latency_ms=(time.perf_counter() - started) * 1000,
                response=response,
            )
            return exc
        except Exception as exc:
            normalized = ModelProviderUnavailableError(
                "The provider returned an unclassified internal failure."
            )
            await self._record_trace(
                request=request,
                context=trace_context,
                provider=provider,
                role=role,
                attempt_no=attempt_no,
                outcome=ModelAttemptOutcome.ERROR,
                error=normalized,
                latency_ms=(time.perf_counter() - started) * 1000,
                response=response,
            )
            raise normalized from exc
        self._metrics.provider_succeeded(role)
        await self._record_trace(
            request=request,
            context=trace_context,
            provider=provider,
            role=role,
            attempt_no=attempt_no,
            outcome=ModelAttemptOutcome.SUCCESS,
            error=None,
            latency_ms=response.latency_ms,
            response=response,
        )
        return ModelGatewayResult(
            request_id=request.request_id,
            value=value,
            provider_name=response.provider_name,
            provider_version=response.provider_version,
            model=response.model,
            fallback_used=False,
            fallback_type=None,
            provider_attempts=attempt_no,
        )

    async def _template_result(
        self,
        *,
        request: ModelRequest,
        trace_context: ModelTraceContext,
        attempt_no: int,
        provider_attempts: int,
        template_factory: Callable[[], T],
    ) -> ModelGatewayResult[T]:
        try:
            value = template_factory()
        except Exception as exc:
            raise ModelProviderUnavailableError(
                "The safe model fallback contract could not be created."
            ) from exc
        self._metrics.template_used()
        trace = ModelCallTrace(
            id=uuid4(),
            request_id=request.request_id,
            user_id=trace_context.user_id,
            agent_name=trace_context.agent_name,
            prompt_name=trace_context.prompt_name,
            prompt_version=trace_context.prompt_version,
            prompt_sha256=trace_context.prompt_sha256,
            provider_name="template-fallback",
            provider_version="phase-3a-v1",
            provider_role=ProviderRole.TEMPLATE,
            model="none",
            attempt_no=attempt_no,
            outcome=ModelAttemptOutcome.FALLBACK,
            error_code=ModelErrorCode.MODEL_FALLBACK_USED,
            error_message="A fixed safe template was used.",
            latency_ms=0,
            input_tokens=None,
            output_tokens=None,
            fallback_used=True,
            fallback_type=FallbackType.TEMPLATE,
            input_fingerprint=trace_context.input_fingerprint,
            scope_guard_blocked=False,
            created_at=utc_now(),
            context_snapshot_reference_id=(trace_context.context_snapshot_reference_id),
            context_fingerprint=trace_context.context_fingerprint,
            context_contract_version=trace_context.context_contract_version,
            context_degraded_mode=trace_context.context_degraded_mode,
        )
        await self._traces.append(trace)
        return ModelGatewayResult(
            request_id=request.request_id,
            value=value,
            provider_name="template-fallback",
            provider_version="phase-3a-v1",
            model="none",
            fallback_used=True,
            fallback_type=FallbackType.TEMPLATE,
            provider_attempts=provider_attempts,
        )

    async def record_scope_block(
        self,
        *,
        request_id: UUID,
        trace_context: ModelTraceContext,
        needs_review: bool = False,
    ) -> None:
        if not needs_review:
            self._metrics.scope_blocked()
        await self._traces.append(
            ModelCallTrace(
                id=uuid4(),
                request_id=request_id,
                user_id=trace_context.user_id,
                agent_name=trace_context.agent_name,
                prompt_name=trace_context.prompt_name,
                prompt_version=trace_context.prompt_version,
                prompt_sha256=trace_context.prompt_sha256,
                provider_name="deterministic-scope-guard",
                provider_version="phase-3a-v1",
                provider_role=ProviderRole.SCOPE_GUARD,
                model="none",
                attempt_no=0,
                outcome=ModelAttemptOutcome.SCOPE_BLOCKED,
                error_code=None,
                error_message=(
                    "The request needs human review."
                    if needs_review
                    else "The request is outside the supported product scope."
                ),
                latency_ms=0,
                input_tokens=None,
                output_tokens=None,
                fallback_used=False,
                fallback_type=None,
                input_fingerprint=trace_context.input_fingerprint,
                scope_guard_blocked=True,
                created_at=utc_now(),
                context_snapshot_reference_id=(
                    trace_context.context_snapshot_reference_id
                ),
                context_fingerprint=trace_context.context_fingerprint,
                context_contract_version=trace_context.context_contract_version,
                context_degraded_mode=trace_context.context_degraded_mode,
            )
        )

    async def list_traces(
        self,
        *,
        user_id: UUID,
        request_id: UUID,
    ) -> tuple[ModelCallTrace, ...]:
        return await self._traces.list_for_request(
            user_id=user_id,
            request_id=request_id,
        )

    def metrics(self) -> ModelGatewayMetricsSnapshot:
        return self._metrics.snapshot()

    async def close(self) -> None:
        providers = dict.fromkeys((self._primary, self._backup))
        for provider in providers:
            if provider is not None:
                await provider.close()

    async def _record_trace(
        self,
        *,
        request: ModelRequest,
        context: ModelTraceContext,
        provider: ModelProvider,
        role: ProviderRole,
        attempt_no: int,
        outcome: ModelAttemptOutcome,
        error: ModelGatewayError | None,
        latency_ms: float,
        response: ModelProviderResponse | None,
    ) -> None:
        if self._observability is not None:
            outcome_value = outcome.value
            labels = {
                "provider_name": provider.provider_name,
                "outcome": outcome_value,
            }
            self._observability.record_counter(
                "fitweek_model_attempts_total", labels=labels
            )
            code = error.code.value if error is not None else None
            if code is not None and "TIMEOUT" in code:
                self._observability.record_counter(
                    "fitweek_model_timeouts_total", labels=labels
                )
            if code is not None and "RATE_LIMIT" in code:
                self._observability.record_counter(
                    "fitweek_model_rate_limits_total", labels=labels
                )
            if code is not None and "5XX" in code:
                self._observability.record_counter(
                    "fitweek_model_5xx_total", labels=labels
                )
            if code is not None and "SCHEMA" in code:
                self._observability.record_counter(
                    "fitweek_model_schema_invalid_total", labels=labels
                )
                self._observability.record_counter(
                    "fitweek_agent_schema_repairs_total",
                    labels={
                        "agent_type": context.agent_name,
                        "outcome": outcome_value,
                    },
                )
            if attempt_no > 1:
                self._observability.record_counter(
                    "fitweek_agent_retries_total",
                    labels={
                        "agent_type": context.agent_name,
                        "outcome": outcome_value,
                    },
                )
            if role is ProviderRole.BACKUP and outcome is ModelAttemptOutcome.SUCCESS:
                self._observability.record_counter(
                    "fitweek_model_backup_used_total", labels=labels
                )
            inherited = current_observability_context()
            with self._observability.start_span(
                "model.attempt",
                context=ObservabilityContext(
                    correlation_id=(
                        inherited.correlation_id
                        if inherited is not None
                        else request.request_id
                    ),
                    request_id=str(request.request_id),
                    run_id=inherited.run_id if inherited is not None else None,
                    step_id=inherited.step_id if inherited is not None else None,
                    operation_name="model.attempt",
                    component="model_gateway",
                ),
                attributes={
                    "provider_name": provider.provider_name,
                    "attempt_number": attempt_no,
                    "outcome": outcome_value,
                    "error_category": code or "NONE",
                },
            ) as span:
                if error is None:
                    span.succeed(outcome_value)
                else:
                    span.fail(code or "MODEL_ERROR", code)
        await self._traces.append(
            ModelCallTrace(
                id=uuid4(),
                request_id=request.request_id,
                user_id=context.user_id,
                agent_name=context.agent_name,
                prompt_name=context.prompt_name,
                prompt_version=context.prompt_version,
                prompt_sha256=context.prompt_sha256,
                provider_name=provider.provider_name,
                provider_version=provider.provider_version,
                provider_role=role,
                model=request.model,
                attempt_no=attempt_no,
                outcome=outcome,
                error_code=error.code if error is not None else None,
                error_message=(redact_text(str(error)) if error is not None else None),
                latency_ms=latency_ms,
                input_tokens=(response.input_tokens if response is not None else None),
                output_tokens=(
                    response.output_tokens if response is not None else None
                ),
                fallback_used=False,
                fallback_type=None,
                input_fingerprint=context.input_fingerprint,
                scope_guard_blocked=False,
                created_at=utc_now(),
                context_snapshot_reference_id=(context.context_snapshot_reference_id),
                context_fingerprint=context.context_fingerprint,
                context_contract_version=context.context_contract_version,
                context_degraded_mode=context.context_degraded_mode,
            )
        )
