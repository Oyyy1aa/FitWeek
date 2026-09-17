"""Telemetry exporter failures cannot alter business execution."""

from collections.abc import Mapping
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from prometheus_client import CollectorRegistry

from app.observability.context import ObservabilityContext
from app.observability.facade import build_observability
from app.observability.logging import InMemoryStructuredLogSink
from app.observability.metrics import PrometheusMetricSink
from app.observability.testing import FailingLogSink

pytestmark = pytest.mark.phase_8b1


def _context() -> ObservabilityContext:
    return ObservabilityContext(
        correlation_id=uuid4(), operation_name="business", component="application"
    )


class CountingFailingExporter(SpanExporter):
    def __init__(self) -> None:
        self.calls = 0

    def export(self, spans: object) -> SpanExportResult:
        self.calls += 1
        raise TimeoutError("collector unavailable")


class CountingFailingLogSink(FailingLogSink):
    def __init__(self) -> None:
        self.calls = 0

    def emit(self, payload: Mapping[str, object]) -> None:
        self.calls += 1
        super().emit(payload)


class FailingRegistry(CollectorRegistry):
    def register(self, collector: object) -> None:
        raise RuntimeError("registration unavailable")


def test_otel_timeout_does_not_fail_business_operation() -> None:
    exporter = CountingFailingExporter()
    facade = build_observability(
        enabled=True,
        tracing_enabled=True,
        exporter_name="in_memory",
        service_name="test",
        metrics_enabled=True,
        structured_logging_enabled=True,
        span_exporter=exporter,
        log_sink=InMemoryStructuredLogSink(),
    )
    side_effects: list[str] = []
    with facade.start_span("business", context=_context()) as span:
        side_effects.append("created-once")
        span.succeed()
    assert side_effects == ["created-once"]
    assert exporter.calls == 1
    assert facade.readiness()["exporter_status"] == "DEGRADED"


def test_otel_exception_does_not_trigger_business_retry() -> None:
    exporter = CountingFailingExporter()
    facade = build_observability(
        enabled=True,
        tracing_enabled=True,
        exporter_name="in_memory",
        service_name="test",
        metrics_enabled=False,
        structured_logging_enabled=False,
        span_exporter=exporter,
    )
    invocations = 0
    with facade.operation("business", context=_context()) as span:
        invocations += 1
        span.succeed()
    assert invocations == 1
    assert exporter.calls == 1


def test_logging_sink_failure_is_isolated() -> None:
    sink = CountingFailingLogSink()
    facade = build_observability(
        enabled=True,
        tracing_enabled=False,
        exporter_name="disabled",
        service_name="test",
        metrics_enabled=False,
        structured_logging_enabled=True,
        log_sink=sink,
    )
    facade.emit_log(
        event_name="failure",
        context=_context(),
        level="ERROR",
        outcome="FAILED",
    )
    assert sink.calls == 1
    assert facade.readiness()["status"] == "DEGRADED"


def test_logging_failure_does_not_recurse() -> None:
    sink = CountingFailingLogSink()
    facade = build_observability(
        enabled=True,
        tracing_enabled=False,
        exporter_name="disabled",
        service_name="test",
        metrics_enabled=False,
        structured_logging_enabled=True,
        log_sink=sink,
    )
    facade.emit_log(
        event_name="one",
        context=_context(),
        level="ERROR",
        outcome="FAILED",
    )
    assert sink.calls == 1


def test_prometheus_registration_failure_becomes_degraded() -> None:
    sink = PrometheusMetricSink(enabled=True, registry=FailingRegistry())
    assert sink.degraded is True
    assert sink.render() == b""


def test_prometheus_recording_failure_does_not_escape() -> None:
    sink = PrometheusMetricSink(enabled=True)
    sink.counter(
        "fitweek_plan_generations_total",
        labels={"outcome": "OK", "component": "invalid-extra"},
    )
    assert sink.degraded is True


def test_disabled_exporter_keeps_business_and_trace_empty() -> None:
    facade = build_observability(
        enabled=True,
        tracing_enabled=True,
        exporter_name="disabled",
        service_name="test",
        metrics_enabled=False,
        structured_logging_enabled=False,
    )
    with facade.start_span("business", context=_context()):
        result = "completed"
    assert result == "completed"
    assert facade.tracing.finished_spans() == ()


def test_shutdown_failure_is_contained_by_safe_exporter() -> None:
    exporter = CountingFailingExporter()
    facade = build_observability(
        enabled=True,
        tracing_enabled=True,
        exporter_name="in_memory",
        service_name="test",
        metrics_enabled=False,
        structured_logging_enabled=False,
        span_exporter=exporter,
    )
    facade.shutdown()
    assert facade.readiness()["status"] in {"AVAILABLE", "DEGRADED"}
