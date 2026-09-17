"""Lifespan-owned, side-effect-free alerting runtime for local evaluation drills."""

from __future__ import annotations

from pathlib import Path

from app.alerting.clock import AlertClock, SystemAlertClock
from app.alerting.evaluator import AlertTransition, LocalAlertEvaluator
from app.alerting.models import AlertRuleDefinition, AlertStatus
from app.alerting.routing import (
    AlertRouter,
    DisabledNotificationSink,
    InhibitionPolicy,
    InMemoryNotificationSink,
    NotificationDispatcher,
    NotificationSink,
    SilenceStore,
)
from app.alerting.validation import AlertingAssetValidator, AssetValidationSummary
from app.observability.facade import ObservabilityFacade


class AlertingRuntime:
    def __init__(
        self,
        *,
        enabled: bool,
        observability: ObservabilityFacade,
        validation: AssetValidationSummary,
        clock: AlertClock,
        sink: NotificationSink,
    ) -> None:
        self.enabled = enabled
        self.observability = observability
        self.validation = validation
        self.clock = clock
        self.evaluator = LocalAlertEvaluator(clock)
        self.router = AlertRouter()
        self.inhibition = InhibitionPolicy()
        self.silences = SilenceStore()
        self.dispatcher = NotificationDispatcher(sink, self.router)

    def evaluate(
        self,
        *,
        rule: AlertRuleDefinition,
        condition: bool,
        labels: tuple[tuple[str, str], ...] = (),
    ) -> AlertTransition:
        transition = self.evaluator.evaluate(
            rule=rule, condition=condition, labels=labels
        )
        instance = transition.current
        metric_labels = {
            "alert_name": rule.alert_name,
            "severity": rule.severity.value,
            "component": rule.component,
            "outcome": instance.status.value,
        }
        self.observability.record_counter(
            "fitweek_alert_evaluations_total", labels=metric_labels
        )
        if (
            transition.previous is AlertStatus.FIRING
            and instance.status is not AlertStatus.FIRING
        ):
            self.observability.record_gauge_delta(
                "fitweek_alert_state",
                -1,
                labels={
                    "alert_name": rule.alert_name,
                    "severity": rule.severity.value,
                    "component": rule.component,
                    "alert_state": AlertStatus.FIRING.value,
                },
            )
        self.observability.record_gauge_delta(
            "fitweek_alert_state",
            1 if instance.status is AlertStatus.FIRING else 0,
            labels={
                "alert_name": rule.alert_name,
                "severity": rule.severity.value,
                "component": rule.component,
                "alert_state": instance.status.value,
            },
        )
        if transition.notification_required:
            active = self.evaluator.instances()
            if self.inhibition.inhibited(instance, active):
                self.observability.record_counter(
                    "fitweek_alert_inhibitions_total",
                    labels={"component": rule.component, "outcome": "INHIBITED"},
                )
            elif self.silences.silenced(instance, self.clock.now()):
                self.observability.record_counter(
                    "fitweek_alert_silences_total",
                    labels={"component": rule.component, "outcome": "SILENCED"},
                )
            else:
                sent, receiver = self.dispatcher.dispatch(instance)
                outcome = "SENT" if sent else "FAILED"
                self.observability.record_counter(
                    "fitweek_alert_notifications_total",
                    labels={
                        "alert_name": rule.alert_name,
                        "severity": rule.severity.value,
                        "component": rule.component,
                        "receiver": receiver,
                        "outcome": outcome,
                    },
                )
                if not sent:
                    self.observability.record_counter(
                        "fitweek_alert_receiver_failures_total",
                        labels={"receiver": receiver, "outcome": "FAILED"},
                    )
        return transition

    def readiness(self) -> dict[str, object]:
        if not self.enabled:
            return {
                "status": "DISABLED",
                "dashboard_definitions_valid": (
                    self.validation.dashboard_definitions_valid
                ),
                "recording_rules_valid": self.validation.recording_rules_valid,
                "alert_rules_valid": self.validation.alert_rules_valid,
                "routing_policy_valid": self.validation.routing_policy_valid,
                "notification_sink": "disabled",
            }
        return {
            "status": "AVAILABLE" if self.validation.valid else "DEGRADED",
            "dashboard_definitions_valid": self.validation.dashboard_definitions_valid,
            "recording_rules_valid": self.validation.recording_rules_valid,
            "alert_rules_valid": self.validation.alert_rules_valid,
            "routing_policy_valid": self.validation.routing_policy_valid,
            "notification_sink": (
                "in_memory"
                if isinstance(self.dispatcher.sink, InMemoryNotificationSink)
                else "disabled"
            ),
        }


def build_alerting_runtime(
    *,
    enabled: bool,
    sink_name: str,
    observability: ObservabilityFacade,
    project_root: Path | None = None,
    clock: AlertClock | None = None,
    sink: NotificationSink | None = None,
) -> AlertingRuntime:
    root = project_root or Path(__file__).resolve().parents[2]
    validation = AlertingAssetValidator(root).validate()
    active_sink = sink
    if active_sink is None:
        active_sink = (
            InMemoryNotificationSink()
            if enabled and sink_name in {"in_memory", "file_test_sink"}
            else DisabledNotificationSink()
        )
    return AlertingRuntime(
        enabled=enabled,
        observability=observability,
        validation=validation,
        clock=clock or SystemAlertClock(),
        sink=active_sink,
    )
