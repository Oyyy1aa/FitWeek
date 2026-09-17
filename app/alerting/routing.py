"""Deterministic routing, inhibition, silence, and bounded notification sinks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from app.alerting.models import (
    AlertInstance,
    AlertNotification,
    AlertSeverity,
    AlertSilence,
    AlertStatus,
)


class NotificationSink(Protocol):
    def send(self, notification: AlertNotification) -> None: ...


class InMemoryNotificationSink:
    def __init__(self) -> None:
        self.notifications: list[AlertNotification] = []

    def send(self, notification: AlertNotification) -> None:
        self.notifications.append(notification)


class DisabledNotificationSink:
    def send(self, notification: AlertNotification) -> None:
        return None


class FailingNotificationSink:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, notification: AlertNotification) -> None:
        self.calls += 1
        raise RuntimeError("TEST_NOTIFICATION_SINK_FAILURE")


class AlertRouter:
    RECEIVERS = frozenset(
        {"default", "warning", "critical", "security", "calendar", "observability"}
    )

    def route(self, instance: AlertInstance) -> str:
        component = instance.rule.component
        if component == "security" and instance.rule.severity is AlertSeverity.CRITICAL:
            return "security"
        if instance.rule.severity is AlertSeverity.CRITICAL:
            return "critical"
        if component == "calendar":
            return "calendar"
        if component == "observability":
            return "observability"
        if instance.rule.severity is AlertSeverity.WARNING:
            return "warning"
        return "default"


class InhibitionPolicy:
    def inhibited(self, target: AlertInstance, active: Sequence[AlertInstance]) -> bool:
        firing_names = {
            item.rule.alert_name
            for item in active
            if item.status is AlertStatus.FIRING
            and item.rule.component == target.rule.component
        }
        if target.rule.component == "security":
            return False
        if (
            target.rule.alert_name == "FitWeekCalendarWriteFailureHigh"
            and "FitWeekCalendarWriteCircuitOpen" in firing_names
        ):
            return True
        if (
            target.rule.alert_name == "FitWeekHttpEndpointFailure"
            and "FitWeekApiUnavailable" in firing_names
        ):
            return True
        return False


class SilenceStore:
    def __init__(self) -> None:
        self._silences: dict[str, AlertSilence] = {}

    def add_internal(self, silence: AlertSilence) -> None:
        self._silences[str(silence.id)] = silence

    def silenced(self, instance: AlertInstance, now: datetime) -> bool:
        if (
            instance.rule.component == "security"
            and instance.rule.severity is AlertSeverity.CRITICAL
        ):
            return False
        values: Mapping[str, str] = {
            "alert_name": instance.rule.alert_name,
            "severity": instance.rule.severity.value,
            "component": instance.rule.component,
            **dict(instance.labels),
        }
        return any(
            silence.starts_at <= now < silence.ends_at
            and all(values.get(item.label) == item.value for item in silence.matcher)
            for silence in self._silences.values()
        )


class NotificationDispatcher:
    """Send FIRING/RESOLVED once; receiver failure is never retried here."""

    def __init__(self, sink: NotificationSink, router: AlertRouter) -> None:
        self.sink = sink
        self.router = router
        self._sent: set[tuple[str, AlertStatus]] = set()
        self.failures = 0

    def dispatch(self, instance: AlertInstance) -> tuple[bool, str]:
        if instance.status not in {AlertStatus.FIRING, AlertStatus.RESOLVED}:
            return False, self.router.route(instance)
        key = (instance.fingerprint, instance.status)
        receiver = self.router.route(instance)
        if key in self._sent:
            return False, receiver
        notification = AlertNotification(
            alert_name=instance.rule.alert_name,
            status=instance.status,
            severity=instance.rule.severity,
            component=instance.rule.component,
            summary=instance.rule.summary,
            safe_description=instance.rule.safe_description,
            runbook_id=instance.rule.runbook_id,
            started_at=instance.active_since or instance.updated_at,
            resolved_at=instance.resolved_at,
            routing_receiver=receiver,
            fingerprint=instance.fingerprint,
        )
        try:
            self.sink.send(notification)
        except Exception:
            self.failures += 1
            return False, receiver
        self._sent.add(key)
        return True, receiver
