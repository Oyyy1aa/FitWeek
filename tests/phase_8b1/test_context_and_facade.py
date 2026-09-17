"""Safe context propagation and exporter-independent facade behavior."""

from uuid import UUID, uuid4

import pytest
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import ValidationError

from app.observability.context import (
    ObservabilityContext,
    current_observability_context,
    reset_observability_context,
    set_observability_context,
)
from app.observability.facade import build_observability, build_test_observability
from app.observability.logging import InMemoryStructuredLogSink

pytestmark = pytest.mark.phase_8b1


def _context() -> ObservabilityContext:
    return ObservabilityContext(
        correlation_id=uuid4(),
        operation_name="test.operation",
        component="test",
    )


def test_context_is_frozen_and_reference_only() -> None:
    context = _context()
    with pytest.raises(ValidationError):
        context.operation_name = "changed"  # type: ignore[misc]
    assert set(context.model_dump()) == {
        "correlation_id",
        "request_id",
        "run_id",
        "step_id",
        "trace_id",
        "span_id",
        "operation_name",
        "component",
    }


def test_context_rejects_blank_operation() -> None:
    with pytest.raises(ValidationError):
        ObservabilityContext(correlation_id=uuid4(), operation_name="", component="api")


def test_context_var_round_trip() -> None:
    context = _context()
    token = set_observability_context(context)
    assert current_observability_context() is context
    reset_observability_context(token)
    assert current_observability_context() is None


def test_context_carries_run_and_step_references() -> None:
    run_id, step_id = uuid4(), uuid4()
    context = ObservabilityContext(
        correlation_id=uuid4(),
        run_id=run_id,
        step_id=step_id,
        operation_name="worker.step",
        component="orchestrator",
    )
    assert context.run_id == run_id
    assert context.step_id == step_id


def test_facade_span_has_trace_and_span_ids() -> None:
    facade = build_test_observability()
    with facade.start_span("test.root", context=_context()) as span:
        assert len(span.trace_id or "") == 32
        assert len(span.span_id or "") == 16
        span.succeed()
    assert len(facade.tracing.finished_spans()) == 1


def test_facade_nested_spans_share_trace_and_parent() -> None:
    facade = build_test_observability()
    with facade.start_span("root", context=_context()) as root:
        with facade.start_span("child", context=_context()) as child:
            child.succeed()
        root.succeed()
    spans = {span.name: span for span in facade.tracing.finished_spans()}
    assert spans["root"].context.trace_id == spans["child"].context.trace_id
    assert spans["child"].parent is not None
    assert spans["child"].parent.span_id == spans["root"].context.span_id


def test_facade_operation_marks_unhandled_failure_without_exception_text() -> None:
    facade = build_test_observability()
    with pytest.raises(RuntimeError):
        with facade.operation("failing", context=_context()):
            raise RuntimeError("private detail")
    span = facade.tracing.finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert "private detail" not in str(span.attributes)
    assert not span.events


def test_disabled_facade_is_noop() -> None:
    facade = build_observability(
        enabled=False,
        tracing_enabled=True,
        exporter_name="in_memory",
        service_name="disabled",
        metrics_enabled=True,
        structured_logging_enabled=True,
    )
    with facade.start_span("disabled", context=_context(), kind=SpanKind.SERVER):
        facade.record_counter(
            "fitweek_plan_generations_total", labels={"outcome": "OK"}
        )
    assert facade.tracing.finished_spans() == ()
    assert facade.metrics_text() == b""
    assert facade.readiness()["status"] == "DISABLED"


def test_facade_emits_stable_in_memory_log() -> None:
    facade = build_test_observability()
    assert isinstance(facade.log_sink, InMemoryStructuredLogSink)
    facade.emit_log(
        event_name="safe_event",
        context=_context(),
        level="INFO",
        outcome="SUCCEEDED",
    )
    item = facade.log_sink.items()[0]
    assert item["event_name"] == "safe_event"
    assert UUID(str(item["correlation_id"]))


def test_readiness_reports_enabled_components() -> None:
    readiness = build_test_observability().readiness()
    assert readiness == {
        "status": "AVAILABLE",
        "tracing_enabled": True,
        "metrics_enabled": True,
        "structured_logging_enabled": True,
        "exporter_status": "AVAILABLE",
    }
