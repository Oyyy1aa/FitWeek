"""Deterministic INACTIVE/PENDING/FIRING/RESOLVED transitions."""

from datetime import UTC, datetime, timedelta

import pytest

from app.alerting.clock import ManualAlertClock
from app.alerting.evaluator import LocalAlertEvaluator
from app.alerting.models import AlertRuleDefinition, AlertSeverity, AlertStatus

pytestmark = pytest.mark.phase_8b2


def _rule(*, delay: int = 300) -> AlertRuleDefinition:
    return AlertRuleDefinition(
        alert_name="FitWeekAgentSuccessRateLow",
        severity=AlertSeverity.WARNING,
        component="agent",
        summary="Agent success low",
        safe_description="The bounded rate is below threshold.",
        runbook_id="agent-success-rate",
        for_seconds=delay,
    )


def test_false_condition_is_inactive() -> None:
    clock = ManualAlertClock()
    result = LocalAlertEvaluator(clock).evaluate(rule=_rule(), condition=False)
    assert result.current.status is AlertStatus.INACTIVE
    assert not result.notification_required


def test_true_condition_enters_pending() -> None:
    evaluator = LocalAlertEvaluator(ManualAlertClock())
    result = evaluator.evaluate(rule=_rule(), condition=True)
    assert result.current.status is AlertStatus.PENDING


def test_pending_remains_before_for_duration() -> None:
    clock = ManualAlertClock()
    evaluator = LocalAlertEvaluator(clock)
    evaluator.evaluate(rule=_rule(), condition=True)
    clock.advance(timedelta(seconds=299))
    assert (
        evaluator.evaluate(rule=_rule(), condition=True).current.status
        is AlertStatus.PENDING
    )


def test_pending_becomes_firing_at_for_duration() -> None:
    clock = ManualAlertClock()
    evaluator = LocalAlertEvaluator(clock)
    evaluator.evaluate(rule=_rule(), condition=True)
    clock.advance(timedelta(seconds=300))
    result = evaluator.evaluate(rule=_rule(), condition=True)
    assert result.current.status is AlertStatus.FIRING
    assert result.notification_required


def test_firing_is_not_re_notified_while_unchanged() -> None:
    evaluator = LocalAlertEvaluator(ManualAlertClock())
    first = evaluator.evaluate(rule=_rule(delay=0), condition=True)
    second = evaluator.evaluate(rule=_rule(delay=0), condition=True)
    assert first.notification_required
    assert not second.notification_required


def test_firing_recovery_enters_resolved() -> None:
    evaluator = LocalAlertEvaluator(ManualAlertClock())
    evaluator.evaluate(rule=_rule(delay=0), condition=True)
    result = evaluator.evaluate(rule=_rule(delay=0), condition=False)
    assert result.current.status is AlertStatus.RESOLVED
    assert result.current.resolved_at is not None
    assert result.notification_required


def test_resolved_then_false_returns_inactive() -> None:
    evaluator = LocalAlertEvaluator(ManualAlertClock())
    evaluator.evaluate(rule=_rule(delay=0), condition=True)
    evaluator.evaluate(rule=_rule(delay=0), condition=False)
    assert (
        evaluator.evaluate(rule=_rule(delay=0), condition=False).current.status
        is AlertStatus.INACTIVE
    )


def test_pending_recovery_returns_inactive_without_notification() -> None:
    evaluator = LocalAlertEvaluator(ManualAlertClock())
    evaluator.evaluate(rule=_rule(), condition=True)
    result = evaluator.evaluate(rule=_rule(), condition=False)
    assert result.current.status is AlertStatus.INACTIVE
    assert not result.notification_required


def test_low_cardinality_labels_define_distinct_instances() -> None:
    evaluator = LocalAlertEvaluator(ManualAlertClock())
    evaluator.evaluate(rule=_rule(delay=0), condition=True, labels=(("outcome", "A"),))
    evaluator.evaluate(rule=_rule(delay=0), condition=True, labels=(("outcome", "B"),))
    assert len(evaluator.instances()) == 2


def test_injected_clock_is_timezone_aware_and_deterministic() -> None:
    initial = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
    clock = ManualAlertClock(initial)
    evaluator = LocalAlertEvaluator(clock)
    result = evaluator.evaluate(rule=_rule(), condition=True)
    assert result.current.active_since == initial


def test_state_query_limit_is_bounded() -> None:
    evaluator = LocalAlertEvaluator(ManualAlertClock())
    with pytest.raises(ValueError, match="between 1 and 500"):
        evaluator.instances(limit=501)
