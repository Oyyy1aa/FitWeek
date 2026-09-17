"""OpenTelemetry span hierarchy, status, and attribute policy."""

from uuid import uuid4

import pytest
from opentelemetry.trace import StatusCode

from app.observability.context import ObservabilityContext
from app.observability.facade import build_test_observability
from app.observability.tracing import SPAN_ATTRIBUTE_ALLOWLIST

pytestmark = pytest.mark.phase_8b1


def _context(component: str = "application") -> ObservabilityContext:
    return ObservabilityContext(
        correlation_id=uuid4(), operation_name="operation", component=component
    )


def _single_span(action: str):
    facade = build_test_observability()
    with facade.start_span("span", context=_context()) as span:
        if action == "success":
            span.succeed()
        elif action == "reject":
            span.reject("BUSINESS_REJECTED", "CONTROLLED")
        elif action == "failure":
            span.fail("TIMEOUT", "UPSTREAM_TIMEOUT")
        else:
            span.degrade("NO_MEMORY")
    return facade.tracing.finished_spans()[0]


def test_success_span_is_ok() -> None:
    assert _single_span("success").status.status_code is StatusCode.OK


def test_business_rejection_is_not_internal_error() -> None:
    span = _single_span("reject")
    assert span.status.status_code is StatusCode.UNSET
    assert span.attributes["outcome"] == "BUSINESS_REJECTED"


def test_timeout_span_is_error() -> None:
    span = _single_span("failure")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error_category"] == "TIMEOUT"


def test_degradation_attribute_is_exported() -> None:
    assert _single_span("degraded").attributes["degradation_mode"] == "NO_MEMORY"


def test_attribute_allowlist_contains_only_contract_fields() -> None:
    assert "tool_id" in SPAN_ATTRIBUTE_ALLOWLIST
    assert "http_route_template" in SPAN_ATTRIBUTE_ALLOWLIST
    assert "user_message" not in SPAN_ATTRIBUTE_ALLOWLIST
    assert "authorization" not in SPAN_ATTRIBUTE_ALLOWLIST


def test_forbidden_attributes_are_dropped() -> None:
    facade = build_test_observability()
    with facade.start_span(
        "safe",
        context=_context(),
        attributes={
            "component": "test",
            "tool_id": "ICS_EXPORT",
            "user_message": "do not export",
            "authorization": "Bearer secret",
        },
    ):
        pass
    attributes = facade.tracing.finished_spans()[0].attributes
    assert attributes["tool_id"] == "ICS_EXPORT"
    assert "user_message" not in attributes
    assert "authorization" not in attributes


def test_context_references_are_span_attributes() -> None:
    run_id, step_id = uuid4(), uuid4()
    facade = build_test_observability()
    context = ObservabilityContext(
        correlation_id=uuid4(),
        run_id=run_id,
        step_id=step_id,
        operation_name="step",
        component="orchestrator",
    )
    with facade.start_span("step", context=context):
        pass
    attributes = facade.tracing.finished_spans()[0].attributes
    assert attributes["run_id"] == str(run_id)
    assert attributes["step_id"] == str(step_id)


def test_three_level_hierarchy_is_preserved() -> None:
    facade = build_test_observability()
    with facade.start_span("http.request", context=_context("api")):
        with facade.start_span("application.operation", context=_context()):
            with facade.start_span("tool.invocation", context=_context("tool")):
                pass
    spans = {span.name: span for span in facade.tracing.finished_spans()}
    assert spans["application.operation"].parent == spans["http.request"].context
    assert spans["tool.invocation"].parent == spans["application.operation"].context


def test_tool_attempt_is_child_of_tool_invocation() -> None:
    facade = build_test_observability()
    with facade.start_span("tool.invocation", context=_context("tool")):
        with facade.start_span("tool.attempt", context=_context("tool")):
            pass
    spans = {span.name: span for span in facade.tracing.finished_spans()}
    invocation = spans["tool.invocation"]
    attempt = spans["tool.attempt"]
    assert attempt.parent == invocation.context


def test_separate_root_spans_have_distinct_trace_ids() -> None:
    facade = build_test_observability()
    with facade.start_span("one", context=_context()):
        pass
    with facade.start_span("two", context=_context()):
        pass
    first, second = facade.tracing.finished_spans()
    assert first.context.trace_id != second.context.trace_id


def test_span_does_not_record_exception_event() -> None:
    span = _single_span("failure")
    assert span.events == ()


def test_agent_model_context_hierarchy() -> None:
    facade = build_test_observability()
    with facade.start_span("agent.invocation", context=_context("agent")):
        with facade.start_span("context.build", context=_context("context")):
            pass
        with facade.start_span(
            "model.gateway.invocation", context=_context("model_gateway")
        ):
            pass
    spans = {span.name: span for span in facade.tracing.finished_spans()}
    assert spans["context.build"].parent == spans["agent.invocation"].context
    assert spans["model.gateway.invocation"].parent == spans["agent.invocation"].context
