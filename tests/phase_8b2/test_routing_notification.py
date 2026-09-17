"""Routing priority, inhibition, Silence restrictions, and safe notification."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.alerting.clock import ManualAlertClock
from app.alerting.models import (
    AlertInstance,
    AlertRuleDefinition,
    AlertSeverity,
    AlertSilence,
    AlertStatus,
    SafeLabelMatcher,
    alert_fingerprint,
)
from app.alerting.routing import (
    AlertRouter,
    FailingNotificationSink,
    InhibitionPolicy,
    InMemoryNotificationSink,
    NotificationDispatcher,
    SilenceStore,
)

pytestmark = pytest.mark.phase_8b2


def _instance(
    name: str,
    *,
    severity: AlertSeverity = AlertSeverity.WARNING,
    component: str = "application",
    status: AlertStatus = AlertStatus.FIRING,
) -> AlertInstance:
    rule = AlertRuleDefinition(
        alert_name=name,
        severity=severity,
        component=component,
        summary="Safe summary",
        safe_description="Safe bounded description.",
        runbook_id="agent-success-rate",
        for_seconds=0,
    )
    now = datetime(2026, 7, 22, tzinfo=UTC)
    return AlertInstance(
        fingerprint=alert_fingerprint(rule, ()),
        rule=rule,
        labels=(),
        status=status,
        active_since=now,
        updated_at=now,
        resolved_at=now if status is AlertStatus.RESOLVED else None,
    )


def test_security_critical_routes_to_security() -> None:
    item = _instance(
        "FitWeekUnauthorizedToolCall",
        severity=AlertSeverity.CRITICAL,
        component="security",
    )
    assert AlertRouter().route(item) == "security"


def test_critical_priority_precedes_component_default() -> None:
    item = _instance(
        "FitWeekCalendarWriteCircuitOpen",
        severity=AlertSeverity.CRITICAL,
        component="calendar",
    )
    assert AlertRouter().route(item) == "critical"


def test_calendar_warning_routes_to_calendar() -> None:
    assert (
        AlertRouter().route(
            _instance("FitWeekCalendarWriteFailureHigh", component="calendar")
        )
        == "calendar"
    )


def test_observability_warning_routes_to_observability() -> None:
    assert (
        AlertRouter().route(
            _instance("FitWeekTelemetryExporterDegraded", component="observability")
        )
        == "observability"
    )


def test_warning_routes_to_warning() -> None:
    assert (
        AlertRouter().route(_instance("FitWeekAgentSuccessRateLow", component="agent"))
        == "warning"
    )


def test_calendar_circuit_inhibits_failure_alert() -> None:
    source = _instance(
        "FitWeekCalendarWriteCircuitOpen",
        severity=AlertSeverity.CRITICAL,
        component="calendar",
    )
    target = _instance("FitWeekCalendarWriteFailureHigh", component="calendar")
    assert InhibitionPolicy().inhibited(target, (source, target))


def test_exporter_degradation_does_not_inhibit_security() -> None:
    source = _instance("FitWeekTelemetryExporterDegraded", component="observability")
    target = _instance(
        "FitWeekUnauthorizedToolCall",
        severity=AlertSeverity.CRITICAL,
        component="security",
    )
    assert not InhibitionPolicy().inhibited(target, (source, target))


def test_firing_notification_sent_once() -> None:
    sink = InMemoryNotificationSink()
    dispatcher = NotificationDispatcher(sink, AlertRouter())
    item = _instance("FitWeekAgentSuccessRateLow")
    assert dispatcher.dispatch(item)[0]
    assert not dispatcher.dispatch(item)[0]
    assert len(sink.notifications) == 1


def test_resolved_notification_sent_once_separately() -> None:
    sink = InMemoryNotificationSink()
    dispatcher = NotificationDispatcher(sink, AlertRouter())
    firing = _instance("FitWeekAgentSuccessRateLow")
    resolved = _instance("FitWeekAgentSuccessRateLow", status=AlertStatus.RESOLVED)
    dispatcher.dispatch(firing)
    dispatcher.dispatch(resolved)
    dispatcher.dispatch(resolved)
    assert [item.status for item in sink.notifications] == [
        AlertStatus.FIRING,
        AlertStatus.RESOLVED,
    ]


def test_pending_notification_is_not_sent() -> None:
    sink = InMemoryNotificationSink()
    dispatcher = NotificationDispatcher(sink, AlertRouter())
    assert not dispatcher.dispatch(
        _instance("FitWeekAgentSuccessRateLow", status=AlertStatus.PENDING)
    )[0]
    assert not sink.notifications


def test_receiver_failure_is_bounded_without_retry() -> None:
    sink = FailingNotificationSink()
    dispatcher = NotificationDispatcher(sink, AlertRouter())
    assert not dispatcher.dispatch(_instance("FitWeekAgentSuccessRateLow"))[0]
    assert sink.calls == dispatcher.failures == 1


def test_silence_requires_expiry_and_safe_matcher() -> None:
    now = ManualAlertClock().now()
    with pytest.raises(ValueError, match="24 hours"):
        AlertSilence(
            id=uuid4(),
            matcher=(SafeLabelMatcher(label="component", value="calendar"),),
            starts_at=now,
            ends_at=now + timedelta(hours=25),
            reason_code="MAINTENANCE",
            created_by="OPERATOR",
        )


def test_security_critical_ignores_ordinary_silence() -> None:
    now = ManualAlertClock().now()
    store = SilenceStore()
    store.add_internal(
        AlertSilence(
            id=uuid4(),
            matcher=(SafeLabelMatcher(label="component", value="security"),),
            starts_at=now,
            ends_at=now + timedelta(hours=1),
            reason_code="MAINTENANCE",
            created_by="OPERATOR",
        )
    )
    item = _instance(
        "FitWeekUnauthorizedToolCall",
        severity=AlertSeverity.CRITICAL,
        component="security",
    )
    assert not store.silenced(item, now)


def test_user_identifier_cannot_be_a_silence_matcher() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        SafeLabelMatcher(label="user_id", value="x")
