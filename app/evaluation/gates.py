"""Versioned evaluation gates with explicit zero-tolerance checks."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

from app.evaluation.models import GateCheck

ZERO_TOLERANCE_GATES = frozenset(
    {
        "hard_constraint_escape_count_max",
        "calendar_side_effect_before_confirmation_count_max",
        "duplicate_calendar_event_count_max",
        "recovery_immutable_violation_count_max",
        "cross_user_memory_leak_count_max",
        "pending_memory_leak_count_max",
        "expired_memory_recall_count_max",
        "deleted_memory_recall_count_max",
        "evaluation_internal_error_count_max",
        "unexpected_side_effect_count_max",
    }
)


class GateConfigurationError(ValueError):
    pass


def load_gates(path: Path) -> dict[str, object]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GateConfigurationError("gate configuration is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
        raise GateConfigurationError("gate configuration requires a version")
    return payload


def evaluate_gates(
    *,
    section: str,
    metrics: dict[str, float],
    configuration: dict[str, object],
) -> tuple[GateCheck, ...]:
    raw = configuration.get(section)
    if not isinstance(raw, dict):
        raise GateConfigurationError(f"missing gate section: {section}")
    checks: list[GateCheck] = []
    for gate, threshold_value in sorted(raw.items()):
        if not isinstance(gate, str) or not isinstance(threshold_value, (int, float)):
            raise GateConfigurationError("gate thresholds must be numeric")
        if gate.endswith("_min"):
            metric = gate.removesuffix("_min")
            comparison = "min"
            actual = metrics.get(metric, 0.0)
            passed = actual >= float(threshold_value)
        elif gate.endswith("_max"):
            metric = gate.removesuffix("_max")
            comparison = "max"
            actual = metrics.get(metric, 0.0)
            passed = actual <= float(threshold_value)
        else:
            raise GateConfigurationError("gate names must end in _min or _max")
        checks.append(
            GateCheck(
                gate=gate,
                passed=passed,
                actual=actual,
                threshold=float(threshold_value),
                comparison=comparison,
                zero_tolerance=gate in ZERO_TOLERANCE_GATES,
            )
        )
    return tuple(checks)


def all_gates_pass(checks: tuple[GateCheck, ...]) -> bool:
    return all(item.passed for item in checks)


def zero_tolerance_passed(checks: tuple[GateCheck, ...]) -> bool:
    return all(item.passed for item in checks if item.zero_tolerance)
