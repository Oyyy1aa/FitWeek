"""Canonical fingerprints and protected controlled-ablation contracts."""

import json

import pytest
import yaml

from app.evaluation.ablations import (
    AblationConfigurationError,
    ablation_is_safe,
    load_ablations,
)
from app.evaluation.determinism import repetitions_are_identical, stable_fingerprint
from app.evaluation.models import AblationMode

pytestmark = pytest.mark.phase_9a


def test_mapping_order_does_not_change_fingerprint() -> None:
    assert stable_fingerprint({"a": 1, "b": 2}) == stable_fingerprint({"b": 2, "a": 1})


def test_set_order_does_not_change_fingerprint() -> None:
    assert stable_fingerprint({"items": {"a", "b"}}) == stable_fingerprint(
        {"items": {"b", "a"}}
    )


def test_repetition_comparison_detects_difference() -> None:
    assert repetitions_are_identical(({"x": 1}, {"x": 1}, {"x": 1}))
    assert not repetitions_are_identical(({"x": 1}, {"x": 2}, {"x": 1}))


@pytest.mark.parametrize("mode", tuple(AblationMode))
def test_all_six_ablations_are_present_and_safe(repository_root, mode) -> None:
    specs = load_ablations(repository_root / "evaluation/configs/ablations.yaml")
    selected = next(item for item in specs if item.mode is mode)
    assert ablation_is_safe(selected)


@pytest.mark.parametrize(
    "protected_field",
    [
        "safety_enabled",
        "permission_gate_enabled",
        "confirmation_gate_enabled",
        "idempotency_enabled",
        "memory_isolation_enabled",
        "calendar_operation_key_enabled",
        "plan_cas_enabled",
    ],
)
def test_protected_gate_cannot_be_disabled(
    tmp_path, repository_root, protected_field
) -> None:
    source = yaml.safe_load(
        (repository_root / "evaluation/configs/ablations.yaml").read_text()
    )
    source["modes"][0][protected_field] = False
    path = tmp_path / "ablations.yaml"
    path.write_text(yaml.safe_dump(source), encoding="utf-8")
    with pytest.raises(AblationConfigurationError):
        load_ablations(path)


@pytest.mark.parametrize("mode", tuple(AblationMode))
def test_formal_ablation_preserves_zero_tolerance(repository_root, mode) -> None:
    report = json.loads(
        (
            repository_root / "evaluation/reports/phase-9a-ablation-results.json"
        ).read_text()
    )
    selected = next(
        item for item in report["comparisons"] if item["mode"] == mode.value
    )
    assert selected["zero_tolerance_passed"] is True


def test_no_memory_ablation_changes_recall_without_leak(repository_root) -> None:
    report = json.loads(
        (
            repository_root / "evaluation/reports/phase-9a-ablation-results.json"
        ).read_text()
    )
    selected = next(
        item for item in report["comparisons"] if item["mode"] == "NO_MEMORY_CONTEXT"
    )
    assert selected["memory_metrics"]["memory_case_pass_rate"] < 1
    assert selected["memory_metrics"]["cross_user_memory_leak_count"] == 0


def test_manual_only_ablation_increases_manual_rate(repository_root) -> None:
    report = json.loads(
        (
            repository_root / "evaluation/reports/phase-9a-ablation-results.json"
        ).read_text()
    )
    values = {item["mode"]: item for item in report["comparisons"]}
    assert (
        values["MANUAL_ONLY_CALENDAR"]["plan_metrics"]["manual_only_rate"]
        > values["FULL"]["plan_metrics"]["manual_only_rate"]
    )
