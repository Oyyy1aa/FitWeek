"""Restricted process-local alert state machine; this is not a PromQL engine."""

from __future__ import annotations

from dataclasses import dataclass

from app.alerting.clock import AlertClock
from app.alerting.models import (
    AlertInstance,
    AlertRuleDefinition,
    AlertStatus,
    alert_fingerprint,
)


@dataclass(frozen=True, slots=True)
class AlertTransition:
    previous: AlertStatus | None
    current: AlertInstance
    notification_required: bool


class LocalAlertEvaluator:
    """Evaluate a precomputed condition and retain only low-cardinality state."""

    def __init__(self, clock: AlertClock) -> None:
        self._clock = clock
        self._instances: dict[str, AlertInstance] = {}

    def evaluate(
        self,
        *,
        rule: AlertRuleDefinition,
        condition: bool,
        labels: tuple[tuple[str, str], ...] = (),
    ) -> AlertTransition:
        normalized = tuple(sorted(labels))
        fingerprint = alert_fingerprint(rule, normalized)
        previous = self._instances.get(fingerprint)
        now = self._clock.now()
        prior_status = previous.status if previous else None
        active_since = previous.active_since if previous else None
        resolved_at = previous.resolved_at if previous else None

        if condition:
            if previous is None or previous.status in {
                AlertStatus.INACTIVE,
                AlertStatus.RESOLVED,
            }:
                active_since = now
                status = (
                    AlertStatus.FIRING if rule.for_seconds == 0 else AlertStatus.PENDING
                )
                resolved_at = None
            elif previous.status is AlertStatus.PENDING:
                assert active_since is not None
                elapsed = (now - active_since).total_seconds()
                status = (
                    AlertStatus.FIRING
                    if elapsed >= rule.for_seconds
                    else AlertStatus.PENDING
                )
            else:
                status = AlertStatus.FIRING
        elif previous is not None and previous.status is AlertStatus.FIRING:
            status = AlertStatus.RESOLVED
            resolved_at = now
        else:
            status = AlertStatus.INACTIVE
            active_since = None
            resolved_at = None

        instance = AlertInstance(
            fingerprint=fingerprint,
            rule=rule,
            labels=normalized,
            status=status,
            active_since=active_since,
            updated_at=now,
            resolved_at=resolved_at,
        )
        self._instances[fingerprint] = instance
        return AlertTransition(
            previous=prior_status,
            current=instance,
            notification_required=(
                status in {AlertStatus.FIRING, AlertStatus.RESOLVED}
                and status is not prior_status
            ),
        )

    def instances(self, *, limit: int = 200) -> tuple[AlertInstance, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("Alert query limit must be between 1 and 500.")
        return tuple(
            sorted(
                self._instances.values(),
                key=lambda item: (item.rule.alert_name, item.fingerprint),
            )[:limit]
        )

    def reset_for_testing(self) -> None:
        self._instances.clear()
