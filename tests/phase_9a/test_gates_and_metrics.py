"""Metric aggregation and versioned gate behavior."""

import json

import pytest

from app.evaluation.gates import (
    GateConfigurationError,
    all_gates_pass,
    evaluate_gates,
    load_gates,
    zero_tolerance_passed,
)
from app.evaluation.metrics import framework_metrics, memory_metrics, plan_metrics

pytestmark = pytest.mark.phase_9a


@pytest.mark.parametrize(
    "section,gate",
    [
        ("plan", "total_cases_min"),
        ("plan", "plan_case_pass_rate_min"),
        ("plan", "hard_constraint_escape_count_max"),
        ("plan", "deterministic_output_rate_min"),
        ("plan", "duplicate_calendar_event_count_max"),
        ("memory", "total_cases_min"),
        ("memory", "memory_case_pass_rate_min"),
        ("memory", "cross_user_memory_leak_count_max"),
        ("memory", "pending_memory_leak_count_max"),
        ("memory", "context_determinism_rate_min"),
        ("framework", "evaluation_internal_error_count_max"),
        ("framework", "unexpected_side_effect_count_max"),
    ],
)
def test_required_gate_exists(repository_root, section, gate) -> None:
    config = load_gates(repository_root / "evaluation/configs/gates.yaml")
    assert gate in config[section]


def test_formal_plan_gates_all_pass(repository_root) -> None:
    report = json.loads(
        (repository_root / "evaluation/reports/phase-9a-plan-results.json").read_text()
    )
    assert report["gate_passed"] is True
    assert all(item["passed"] for item in report["gates"])


def test_formal_memory_gates_all_pass(repository_root) -> None:
    report = json.loads(
        (
            repository_root / "evaluation/reports/phase-9a-memory-results.json"
        ).read_text()
    )
    assert report["gate_passed"] is True
    assert all(item["passed"] for item in report["gates"])


def test_zero_tolerance_failure_cannot_be_hidden() -> None:
    config = {
        "version": "test",
        "plan": {"hard_constraint_escape_count_max": 0},
    }
    checks = evaluate_gates(
        section="plan",
        metrics={"hard_constraint_escape_count": 1},
        configuration=config,
    )
    assert not all_gates_pass(checks)
    assert not zero_tolerance_passed(checks)


def test_pass_rate_does_not_override_zero_tolerance_failure() -> None:
    config = {
        "version": "test",
        "plan": {
            "plan_case_pass_rate_min": 0.98,
            "hard_constraint_escape_count_max": 0,
        },
    }
    checks = evaluate_gates(
        section="plan",
        metrics={"plan_case_pass_rate": 1.0, "hard_constraint_escape_count": 1},
        configuration=config,
    )
    assert [item.passed for item in checks] == [False, True]


def test_missing_gate_section_is_rejected() -> None:
    with pytest.raises(GateConfigurationError, match="missing"):
        evaluate_gates(section="plan", metrics={}, configuration={"version": "x"})


def test_invalid_gate_suffix_is_rejected() -> None:
    with pytest.raises(GateConfigurationError, match="end"):
        evaluate_gates(
            section="plan",
            metrics={},
            configuration={"version": "x", "plan": {"invalid": 1}},
        )


def test_empty_metric_aggregates_do_not_divide_by_zero() -> None:
    assert plan_metrics(())["plan_case_pass_rate"] == 0
    assert memory_metrics(())["memory_case_pass_rate"] == 0
    assert framework_metrics(())["evaluation_internal_error_count"] == 0
