"""OpenTelemetry tracing with an isolated in-memory exporter."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from app.observability.redaction import safe_attributes

SPAN_ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "component",
        "operation",
        "workflow_type",
        "step_type",
        "agent_type",
        "tool_id",
        "tool_version",
        "provider_name",
        "outcome",
        "error_category",
        "error_code",
        "degradation_mode",
        "circuit_state",
        "attempt_number",
        "http_method",
        "http_route_template",
        "http_status_code",
        "correlation_id",
        "run_id",
        "step_id",
        "dataset",
        "category",
        "case_id",
        "case_status",
        "failure_category",
        "ablation",
    }
)


class ExportFailureCallback(Protocol):
    def __call__(self, component: str) -> None: ...


class SafeSpanExporter(SpanExporter):
    """Convert exporter failures into degradation without recursive telemetry."""

    def __init__(
        self, delegate: SpanExporter, on_failure: ExportFailureCallback
    ) -> None:
        self._delegate = delegate
        self._on_failure = on_failure

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            return self._delegate.export(spans)
        except Exception:
            self._on_failure("tracing")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._delegate.shutdown()
        except Exception:
            self._on_failure("tracing")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            return self._delegate.force_flush(timeout_millis)
        except Exception:
            self._on_failure("tracing")
            return False


@dataclass(slots=True)
class SpanHandle:
    span: Span | None

    @property
    def trace_id(self) -> str | None:
        if self.span is None:
            return None
        value = self.span.get_span_context().trace_id
        return f"{value:032x}" if value else None

    @property
    def span_id(self) -> str | None:
        if self.span is None:
            return None
        value = self.span.get_span_context().span_id
        return f"{value:016x}" if value else None

    def set_attributes(self, values: Mapping[str, object]) -> None:
        if self.span is None:
            return
        for key, value in safe_attributes(values, SPAN_ATTRIBUTE_ALLOWLIST).items():
            self.span.set_attribute(key, value)

    def succeed(self, outcome: str = "SUCCEEDED") -> None:
        if self.span is not None:
            self.span.set_attribute("outcome", outcome)
            self.span.set_status(Status(StatusCode.OK))

    def reject(self, outcome: str, error_code: str | None = None) -> None:
        if self.span is not None:
            self.span.set_attribute("outcome", outcome)
            if error_code:
                self.span.set_attribute("error_code", error_code)

    def fail(self, category: str, code: str | None = None) -> None:
        if self.span is not None:
            self.span.set_attribute("error_category", category)
            if code:
                self.span.set_attribute("error_code", code)
            self.span.set_status(Status(StatusCode.ERROR))

    def degrade(self, mode: str) -> None:
        if self.span is not None:
            self.span.set_attribute("degradation_mode", mode)


class OpenTelemetryTraceSink:
    def __init__(
        self,
        *,
        enabled: bool,
        exporter_name: str,
        service_name: str,
        on_failure: ExportFailureCallback,
        exporter: SpanExporter | None = None,
    ) -> None:
        self.enabled = enabled and exporter_name != "disabled"
        self.exporter_name = exporter_name
        self._memory_exporter: InMemorySpanExporter | None = None
        self._provider: TracerProvider | None = None
        self._tracer: trace.Tracer | None = None
        if not self.enabled:
            return
        delegate = exporter
        if delegate is None:
            self._memory_exporter = InMemorySpanExporter()
            delegate = self._memory_exporter
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        provider.add_span_processor(
            SimpleSpanProcessor(SafeSpanExporter(delegate, on_failure))
        )
        self._provider = provider
        self._tracer = provider.get_tracer("fitweek", "phase-8b1-v1")

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        attributes: Mapping[str, object] | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
    ) -> Iterator[SpanHandle]:
        if self._tracer is None:
            yield SpanHandle(None)
            return
        with self._tracer.start_as_current_span(
            name,
            kind=kind,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            handle = SpanHandle(span)
            if attributes:
                handle.set_attributes(attributes)
            try:
                yield handle
            except Exception as exc:
                handle.fail("INTERNAL_ERROR", type(exc).__name__)
                raise

    def finished_spans(self) -> tuple[ReadableSpan, ...]:
        if self._memory_exporter is None:
            return ()
        return tuple(self._memory_exporter.get_finished_spans())

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()
