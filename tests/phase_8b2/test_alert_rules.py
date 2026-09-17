"""Alert-rule thresholds, metadata, runbooks, and low-traffic damping."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.alerting.validation import FORBIDDEN_ASSET_TERMS, AlertingAssetValidator

pytestmark = pytest.mark.phase_8b2

REQUIRED_ALERTS = (
    "FitWeekAgentSuccessRateLow",
    "FitWeekAgentSuccessRateCritical",
    "FitWeekModelConsecutiveFailures",
    "FitWeekModelSchemaInvalidHigh",
    "FitWeekWorkerBacklogHigh",
    "FitWeekCalendarWriteFailureHigh",
    "FitWeekCalendarWriteCircuitOpen",
    "FitWeekCalendarReadCircuitOpen",
    "FitWeekCalendarManualOnlyHigh",
    "FitWeekIdempotencyConflictSpike",
    "FitWeekExpiredMemoryRecalled",
    "FitWeekDeletedMemoryRecalled",
    "FitWeekNoMemoryRateHigh",
    "FitWeekUnauthorizedToolCall",
    "FitWeekTelemetryExporterDegraded",
    "FitWeekMetricsEndpointUnavailable",
    "FitWeekStructuredLogSinkFailure",
    "FitWeekToolTimeoutRateHigh",
    "FitWeekToolDeadlineExceededHigh",
    "FitWeekToolBulkheadRejectionsHigh",
    "FitWeekToolRetryBudgetExhausted",
)


def _rules(doc: dict[str, object]) -> Iterator[dict[str, object]]:
    for group in doc["groups"]:  # type: ignore[index]
        yield from group["rules"]


@pytest.mark.parametrize("alert_name", REQUIRED_ALERTS)
def test_required_alert_exists(alert_rules: dict[str, object], alert_name: str) -> None:
    assert alert_name in {str(item["alert"]) for item in _rules(alert_rules)}


def test_alert_names_are_unique(alert_rules: dict[str, object]) -> None:
    names = [item["alert"] for item in _rules(alert_rules)]
    assert len(names) == len(set(names))


def test_every_alert_has_required_labels_and_annotations(
    alert_rules: dict[str, object],
) -> None:
    for rule in _rules(alert_rules):
        assert rule["labels"]["severity"] in {"warning", "critical"}
        assert rule["labels"]["component"]
        assert {"summary", "description", "runbook_url"} <= set(rule["annotations"])
        assert rule["expr"] and rule["for"]


def test_every_alert_runbook_exists(
    alert_rules: dict[str, object], project_root: Path
) -> None:
    for rule in _rules(alert_rules):
        target = rule["annotations"]["runbook_url"]
        path, _, anchor = target.partition("#")
        runbook = project_root / path
        assert runbook.is_file()
        if anchor:
            heading = anchor.replace("-", " ").casefold()
            assert heading in runbook.read_text(encoding="utf-8").casefold()


def test_alert_rules_have_no_forbidden_high_cardinality_terms(
    alert_rules: dict[str, object],
) -> None:
    raw = str(alert_rules).casefold()
    assert not any(term in raw for term in FORBIDDEN_ASSET_TERMS)


def test_low_rate_alerts_have_minimum_traffic_guards(
    alert_rules: dict[str, object],
) -> None:
    guarded = {
        "FitWeekAgentSuccessRateLow",
        "FitWeekAgentSuccessRateCritical",
        "FitWeekModelConsecutiveFailures",
        "FitWeekModelSchemaInvalidHigh",
        "FitWeekCalendarWriteFailureHigh",
        "FitWeekNoMemoryRateHigh",
        "FitWeekToolTimeoutRateHigh",
    }
    rules = {str(item["alert"]): str(item["expr"]) for item in _rules(alert_rules)}
    assert all("increase(" in rules[name] for name in guarded)


def test_documented_thresholds_match_rules(alert_rules: dict[str, object]) -> None:
    rules = {str(item["alert"]): str(item["expr"]) for item in _rules(alert_rules)}
    assert "0.90" in rules["FitWeekAgentSuccessRateLow"]
    assert "0.70" in rules["FitWeekAgentSuccessRateCritical"]
    assert "0.10" in rules["FitWeekModelSchemaInvalidHigh"]
    assert "> 120" in rules["FitWeekWorkerBacklogHigh"]
    assert "0.05" in rules["FitWeekCalendarWriteFailureHigh"]


def test_expired_deleted_and_security_alerts_are_critical(
    alert_rules: dict[str, object],
) -> None:
    rules = {str(item["alert"]): item for item in _rules(alert_rules)}
    for name in (
        "FitWeekExpiredMemoryRecalled",
        "FitWeekDeletedMemoryRecalled",
        "FitWeekUnauthorizedToolCall",
    ):
        assert rules[name]["labels"]["severity"] == "critical"


def test_alert_rules_pass_strict_asset_validation(project_root: Path) -> None:
    result = AlertingAssetValidator(project_root).validate()
    assert result.alert_rules_valid
    assert not result.errors
