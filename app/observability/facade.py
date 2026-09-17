"""Exporter-independent observability facade used by application boundaries."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter

from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.trace import SpanKind

from app.observability.context import ObservabilityContext
from app.observability.logging import (
    InMemoryStructuredLogSink,
    JsonStructuredLogSink,
    StructuredLogSink,
)
from app.observability.metrics import PrometheusMetricSink
from app.observability.tracing import OpenTelemetryTraceSink, SpanHandle


@dataclass(slots=True)
class ObservabilityFacade:
    enabled: bool
    tracing: OpenTelemetryTraceSink
    metrics: PrometheusMetricSink
    log_sink: StructuredLogSink
    structured_logging_enabled: bool
    _degraded_components: set[str] = field(default_factory=set)

    def mark_degraded(self, component: str) -> None:
        self._degraded_components.add(component)

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        context: ObservabilityContext | None = None,
        attributes: Mapping[str, object] | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
    ) -> Iterator[SpanHandle]:
        values = dict(attributes or {})
        if context is not None:
            values.update(
                {
                    "component": context.component,
                    "operation": context.operation_name,
                    "correlation_id": str(context.correlation_id),
                    "run_id": str(context.run_id) if context.run_id else None,
                    "step_id": str(context.step_id) if context.step_id else None,
                }
            )
        try:
            with self.tracing.start_span(name, attributes=values, kind=kind) as span:
                yield span
        except Exception:
            raise

    @contextmanager
    def operation(
        self,
        name: str,
        *,
        context: ObservabilityContext,
        attributes: Mapping[str, object] | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
    ) -> Iterator[SpanHandle]:
        started = perf_counter()
        with self.start_span(
            name, context=context, attributes=attributes, kind=kind
        ) as span:
            try:
                yield span
            except Exception as exc:
                self.emit_log(
                    event_name="operation_failed",
                    context=context,
                    level="ERROR",
                    outcome="FAILED",
                    error_category="INTERNAL_ERROR",
                    error_code=type(exc).__name__,
                    duration_ms=(perf_counter() - started) * 1000,
                    span=span,
                )
                raise

    def record_counter(
        self, name: str, value: float = 1, *, labels: Mapping[str, str] | None = None
    ) -> None:
        try:
            self.metrics.counter(name, value, labels=labels)
            if self.metrics.degraded:
                self.mark_degraded("metrics")
        except Exception:
            self.mark_degraded("metrics")

    def record_histogram(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        try:
            self.metrics.histogram(name, value, labels=labels)
            if self.metrics.degraded:
                self.mark_degraded("metrics")
        except Exception:
            self.mark_degraded("metrics")

    def record_gauge_delta(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        try:
            self.metrics.gauge_add(name, value, labels=labels)
            if self.metrics.degraded:
                self.mark_degraded("metrics")
        except Exception:
            self.mark_degraded("metrics")

    def emit_log(
        self,
        *,
        event_name: str,
        context: ObservabilityContext,
        level: str,
        outcome: str,
        error_category: str | None = None,
        error_code: str | None = None,
        degradation_mode: str | None = None,
        duration_ms: float | None = None,
        span: SpanHandle | None = None,
    ) -> None:
        if not self.structured_logging_enabled:
            return
        payload = {
            "level": level,
            "event_name": event_name,
            "component": context.component,
            "operation": context.operation_name,
            "outcome": outcome,
            "correlation_id": str(context.correlation_id),
            "trace_id": span.trace_id if span else context.trace_id,
            "span_id": span.span_id if span else context.span_id,
            "run_id": str(context.run_id) if context.run_id else None,
            "step_id": str(context.step_id) if context.step_id else None,
            "error_category": error_category,
            "error_code": error_code,
            "degradation_mode": degradation_mode,
            "duration_ms": duration_ms,
        }
        try:
            self.log_sink.emit(payload)
        except Exception:
            # Never recurse into the same failing log sink.
            self.mark_degraded("structured_logging")
            try:
                self.metrics.counter(
                    "fitweek_structured_log_sink_failures_total",
                    labels={"component": "structured_logging", "outcome": "FAILED"},
                )
            except Exception:
                self.mark_degraded("metrics")

    def metrics_text(self) -> bytes:
        return self.metrics.render()

    def readiness(self) -> dict[str, object]:
        degraded = bool(self._degraded_components) or self.metrics.degraded
        return {
            "status": "DEGRADED"
            if degraded
            else ("AVAILABLE" if self.enabled else "DISABLED"),
            "tracing_enabled": self.tracing.enabled,
            "metrics_enabled": self.metrics.enabled,
            "structured_logging_enabled": self.structured_logging_enabled,
            "exporter_status": "DEGRADED"
            if degraded
            else ("AVAILABLE" if self.enabled else "DISABLED"),
        }

    def shutdown(self) -> None:
        try:
            self.tracing.shutdown()
        except Exception:
            self.mark_degraded("tracing")


def build_observability(
    *,
    enabled: bool,
    tracing_enabled: bool,
    exporter_name: str,
    service_name: str,
    metrics_enabled: bool,
    structured_logging_enabled: bool,
    span_exporter: SpanExporter | None = None,
    log_sink: StructuredLogSink | None = None,
) -> ObservabilityFacade:
    degraded: set[str] = set()

    def mark(component: str) -> None:
        degraded.add(component)

    tracing = OpenTelemetryTraceSink(
        enabled=enabled and tracing_enabled,
        exporter_name=exporter_name,
        service_name=service_name,
        on_failure=mark,
        exporter=span_exporter,
    )
    metrics = PrometheusMetricSink(enabled=enabled and metrics_enabled)
    facade = ObservabilityFacade(
        enabled=enabled,
        tracing=tracing,
        metrics=metrics,
        log_sink=log_sink or JsonStructuredLogSink(),
        structured_logging_enabled=enabled and structured_logging_enabled,
        _degraded_components=degraded,
    )
    if metrics.degraded:
        facade.mark_degraded("metrics")
    facade.record_gauge_delta(
        "fitweek_metrics_endpoint_available",
        1 if enabled and metrics_enabled and not metrics.degraded else 0,
        labels={"component": "observability"},
    )
    return facade


def build_test_observability() -> ObservabilityFacade:
    return build_observability(
        enabled=True,
        tracing_enabled=True,
        exporter_name="in_memory",
        service_name="fitweek-test",
        metrics_enabled=True,
        structured_logging_enabled=True,
        log_sink=InMemoryStructuredLogSink(),
    )
