"""Local incident drills driven by real FitWeek HTTP metrics and safe harnesses."""

import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from app.alerting.clock import ManualAlertClock
from app.alerting.evaluator import LocalAlertEvaluator
from app.alerting.models import AlertRuleDefinition, AlertSeverity, AlertStatus
from app.alerting.routing import FailingNotificationSink, InMemoryNotificationSink
from app.alerting.runtime import AlertingRuntime, build_alerting_runtime
from app.api.dependencies import BusinessContainer, build_memory_container
from app.config import get_settings
from app.domain.tools.enums import ToolCaller, ToolErrorCategory, ToolId
from app.domain.tools.models import ToolInvocationContext
from app.main import create_application
from app.observability.facade import build_test_observability
from app.observability.tracing import OpenTelemetryTraceSink
from app.tool_adapters.contracts import CalendarCommitRequest
from tests.phase_8a.test_real_http_fault_closure import (
    _assert_clean_logs,
    _calendar_operation,
    _confirmed_plan,
    _enqueue_calendar_run,
    _running_stack,
    _wait_for_open_circuit_recovery,
)

pytestmark = pytest.mark.phase_8b2


class DrillFailingExporter(SpanExporter):
    def __init__(self) -> None:
        self.calls = 0

    def export(self, spans: object) -> SpanExportResult:
        self.calls += 1
        raise TimeoutError("TEST_COLLECTOR_UNAVAILABLE")


def _rule(
    name: str,
    *,
    severity: AlertSeverity,
    component: str,
    delay: int = 0,
) -> AlertRuleDefinition:
    return AlertRuleDefinition(
        alert_name=name,
        severity=severity,
        component=component,
        summary=f"{name} drill",
        safe_description="A controlled low-cardinality drill condition is active.",
        runbook_id=(
            "unauthorized-tool-call"
            if component == "security"
            else "observability-exporter-failure"
            if component == "observability"
            else "calendar-write-failure"
            if component == "calendar"
            else "memory-safety"
            if component == "memory"
            else "agent-success-rate"
        ),
        for_seconds=delay,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def real_fitweek() -> Iterator[tuple[FastAPI, httpx.Client]]:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("MODEL_PRIMARY_PROVIDER", "scripted-fake")
    monkeypatch.setenv("ALERTING_ENABLED", "true")
    get_settings.cache_clear()
    application = create_application()
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            application, host="127.0.0.1", port=port, log_config=None, access_log=False
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
        yield application, client
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()
    monkeypatch.undo()
    get_settings.cache_clear()


def test_drill_a_agent_rate_inactive_pending_firing_resolved(real_fitweek) -> None:
    application, client = real_fitweek
    for _ in range(5):
        assert client.post("/api/v1/profile-agent/parse", json={}).status_code == 422
    metrics = client.get("/metrics").text
    assert 'agent_type="PROFILE_AGENT",outcome="REJECTED"' in metrics
    runtime: AlertingRuntime = application.state.alerting
    clock = ManualAlertClock(datetime(2026, 7, 22, tzinfo=UTC))
    runtime.clock = clock
    runtime.evaluator = LocalAlertEvaluator(clock)
    rule = _rule(
        "FitWeekAgentSuccessRateLow",
        severity=AlertSeverity.WARNING,
        component="agent",
        delay=300,
    )
    assert (
        runtime.evaluate(rule=rule, condition=False).current.status
        is AlertStatus.INACTIVE
    )
    assert (
        runtime.evaluate(rule=rule, condition=True).current.status
        is AlertStatus.PENDING
    )
    clock.advance(timedelta(minutes=5))
    assert (
        runtime.evaluate(rule=rule, condition=True).current.status is AlertStatus.FIRING
    )
    assert (
        runtime.evaluate(rule=rule, condition=False).current.status
        is AlertStatus.RESOLVED
    )
    sink = runtime.dispatcher.sink
    assert isinstance(sink, InMemoryNotificationSink)
    assert [item.status for item in sink.notifications[-2:]] == [
        AlertStatus.FIRING,
        AlertStatus.RESOLVED,
    ]


def test_drill_b_calendar_write_circuit_and_inhibition(tmp_path) -> None:
    with _running_stack(
        tmp_path, write=True, failure_threshold=1, orchestrator=True
    ) as (api, stub, logs):
        plan = _confirmed_plan(api)
        draft = _calendar_operation(api, plan, "phase-8b2-calendar-drill")
        before = stub.get("/admin/events").json()
        assert (
            api.post(
                f"/api/v1/calendar-operation-drafts/{draft['id']}/execute"
            ).status_code
            == 409
        )
        assert (
            api.post(
                f"/api/v1/calendar-operation-drafts/{draft['id']}/retry"
            ).status_code
            == 409
        )
        assert stub.get("/admin/events").json() == before
        stub.post(
            "/admin/fail-next",
            json={"status_code": 500, "after_commit": False, "count": 3},
        )
        run = _enqueue_calendar_run(api, draft, "phase-8b2-calendar-drill-run")
        assert (
            _wait_for_open_circuit_recovery(
                api, stub, run["id"], draft["id"], expected_write_count=3
            )["status"]
            == "COMPLETED"
        )
        result = api.get(f"/api/v1/calendar-operation-drafts/{draft['id']}")
        assert result.status_code == 200 and result.json()["status"] == "SUCCEEDED"
        assert "fitweek_tool_circuit_open_total" in api.get("/metrics").text
        facade = build_test_observability()
        runtime = build_alerting_runtime(
            enabled=True, sink_name="in_memory", observability=facade
        )
        circuit = runtime.evaluate(
            rule=_rule(
                "FitWeekCalendarWriteCircuitOpen",
                severity=AlertSeverity.CRITICAL,
                component="calendar",
            ),
            condition=True,
        ).current
        failure = runtime.evaluate(
            rule=_rule(
                "FitWeekCalendarWriteFailureHigh",
                severity=AlertSeverity.WARNING,
                component="calendar",
            ),
            condition=True,
        ).current
        assert runtime.inhibition.inhibited(failure, (circuit, failure))
    _assert_clean_logs(logs)


@pytest.mark.asyncio
async def test_drill_c_unauthorized_tool_call_routes_security() -> None:
    settings = get_settings().model_copy(update={"persistence_backend": "memory"})
    container: BusinessContainer = build_memory_container(settings)
    now = container.tool_gateway.clock.now()
    tool_version = next(
        item.descriptor.version
        for item in container.tool_gateway.registry.registrations()
        if item.descriptor.tool_id is ToolId.CALENDAR_COMMIT
    )
    context = ToolInvocationContext(
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        user_id=uuid4(),
        caller=ToolCaller.PROFILE_APPLICATION,
        tool_id=ToolId.CALENDAR_COMMIT,
        tool_version=tool_version,
        deadline_at=now + timedelta(seconds=1),
        created_at=now,
        idempotency_key="safe-test-reference",
    )
    request = CalendarCommitRequest(
        calendar_id="primary",
        operation_key="safe-test-reference",
        operation="DELETE",
        external_event_id="event-reference",
    )
    result = await container.tool_gateway.invoke(context, request)
    assert result.result.error_category is ToolErrorCategory.PERMISSION
    assert result.result.attempt_count == 0
    runtime = build_alerting_runtime(
        enabled=True, sink_name="in_memory", observability=container.observability
    )
    transition = runtime.evaluate(
        rule=_rule(
            "FitWeekUnauthorizedToolCall",
            severity=AlertSeverity.CRITICAL,
            component="security",
        ),
        condition=True,
    )
    assert transition.current.status is AlertStatus.FIRING
    sink = runtime.dispatcher.sink
    assert isinstance(sink, InMemoryNotificationSink)
    assert sink.notifications[-1].routing_receiver == "security"
    assert "safe-test-reference" not in sink.notifications[-1].model_dump_json()


def test_drill_d_expired_memory_alert_does_not_modify_memory(real_fitweek) -> None:
    application, client = real_fitweek
    container = application.state.business_container
    assert isinstance(container, BusinessContainer)
    before = len(container.memory_repository._store._memories)  # noqa: SLF001
    container.observability.record_counter(
        "fitweek_memory_expired_recall_total", labels={"outcome": "INVALID_RECALL"}
    )
    assert "fitweek_memory_expired_recall_total" in client.get("/metrics").text
    runtime: AlertingRuntime = application.state.alerting
    result = runtime.evaluate(
        rule=_rule(
            "FitWeekExpiredMemoryRecalled",
            severity=AlertSeverity.CRITICAL,
            component="memory",
        ),
        condition=True,
    )
    assert result.current.status is AlertStatus.FIRING
    assert len(container.memory_repository._store._memories) == before  # noqa: SLF001


def test_drill_e_observability_failure_keeps_http_success(real_fitweek) -> None:
    application, client = real_fitweek
    facade = application.state.observability
    original = facade.tracing
    exporter = DrillFailingExporter()
    facade.tracing = OpenTelemetryTraceSink(
        enabled=True,
        exporter_name="in_memory",
        service_name="fitweek-drill",
        on_failure=facade.mark_degraded,
        exporter=exporter,
    )
    try:
        assert client.get("/health/live").status_code == 200
        assert exporter.calls >= 1
        assert facade.readiness()["exporter_status"] == "DEGRADED"
        runtime: AlertingRuntime = application.state.alerting
        fired = runtime.evaluate(
            rule=_rule(
                "FitWeekTelemetryExporterDegraded",
                severity=AlertSeverity.WARNING,
                component="observability",
            ),
            condition=True,
        )
        assert fired.current.status is AlertStatus.FIRING
        resolved = runtime.evaluate(rule=fired.current.rule, condition=False)
        assert resolved.current.status is AlertStatus.RESOLVED
    finally:
        facade.tracing.shutdown()
        facade.tracing = original


def test_drill_f_receiver_failure_is_bounded_and_business_continues(
    real_fitweek,
) -> None:
    application, client = real_fitweek
    runtime = build_alerting_runtime(
        enabled=True,
        sink_name="in_memory",
        observability=application.state.observability,
        sink=FailingNotificationSink(),
    )
    result = runtime.evaluate(
        rule=_rule(
            "FitWeekAgentSuccessRateLow",
            severity=AlertSeverity.WARNING,
            component="agent",
        ),
        condition=True,
    )
    assert result.current.status is AlertStatus.FIRING
    assert runtime.dispatcher.failures == 1
    assert client.get("/health/live").status_code == 200
    assert runtime.evaluator.instances()[0].status is AlertStatus.FIRING
