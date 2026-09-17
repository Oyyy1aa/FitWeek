"""Recording-rule structure, dimensions, and denominator safety."""

from collections.abc import Iterator

import pytest

from app.alerting.validation import AlertingAssetValidator
from app.observability.metrics import DEFAULT_METRIC_DESCRIPTORS

pytestmark = pytest.mark.phase_8b2

REQUIRED_RECORDS = (
    "fitweek:http_success_rate_5m",
    "fitweek:http_5xx_rate_5m",
    "fitweek:http_p95_seconds_5m",
    "fitweek:agent_success_rate_5m",
    "fitweek:agent_p95_seconds_5m",
    "fitweek:model_failure_rate_5m",
    "fitweek:model_timeout_rate_5m",
    "fitweek:model_schema_invalid_rate_5m",
    "fitweek:tool_success_rate_5m",
    "fitweek:tool_failure_rate_5m",
    "fitweek:tool_timeout_rate_5m",
    "fitweek:tool_retry_rate_5m",
    "fitweek:tool_p95_seconds_5m",
    "fitweek:calendar_write_failure_rate_5m",
    "fitweek:memory_no_memory_rate_5m",
    "fitweek:recovery_failure_rate_5m",
)


def _rules(doc: dict[str, object]) -> Iterator[dict[str, object]]:
    for group in doc["groups"]:  # type: ignore[index]
        yield from group["rules"]


@pytest.mark.parametrize("record_name", REQUIRED_RECORDS)
def test_required_recording_rule_exists(
    recording_rules: dict[str, object], record_name: str
) -> None:
    assert record_name in {str(item["record"]) for item in _rules(recording_rules)}


def test_recording_group_names_are_unique(recording_rules: dict[str, object]) -> None:
    groups = recording_rules["groups"]
    names = [item["name"] for item in groups]  # type: ignore[index]
    assert len(names) == len(set(names))


def test_recording_rule_names_are_unique(recording_rules: dict[str, object]) -> None:
    names = [item["record"] for item in _rules(recording_rules)]
    assert len(names) == len(set(names))


def test_divisions_guard_zero_denominators(recording_rules: dict[str, object]) -> None:
    for rule in _rules(recording_rules):
        expr = str(rule["expr"])
        if "/" in expr:
            assert "clamp_min" in expr


def test_histogram_quantiles_group_by_le(recording_rules: dict[str, object]) -> None:
    expressions = [str(rule["expr"]) for rule in _rules(recording_rules)]
    for expr in expressions:
        if "histogram_quantile" in expr:
            assert "le" in expr and "_bucket" in expr


def test_rate_only_references_counter_or_histogram_bucket(
    recording_rules: dict[str, object],
) -> None:
    kinds = {item.name: item.kind.value for item in DEFAULT_METRIC_DESCRIPTORS}
    assert kinds["http_server_requests_total"] == "counter"
    assert kinds["fitweek_tool_duration_seconds"] == "histogram"
    assert all(
        "rate(" in str(rule["expr"]) or "histogram_quantile" in str(rule["expr"])
        for rule in _rules(recording_rules)
    )


def test_recording_rules_pass_strict_asset_validation(project_root) -> None:
    result = AlertingAssetValidator(project_root).validate()
    assert result.recording_rules_valid
    assert not result.errors
