"""Controlled ablation contracts; protected gates can never be disabled."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError

from app.evaluation.models import AblationMode, AblationSpec


class AblationConfigurationError(ValueError):
    pass


def load_ablations(path: Path) -> tuple[AblationSpec, ...]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        modes = payload["modes"]
        values = TypeAdapter(tuple[AblationSpec, ...]).validate_python(modes)
    except (OSError, KeyError, TypeError, yaml.YAMLError, ValidationError) as exc:
        raise AblationConfigurationError("ablation configuration is invalid") from exc
    if {item.mode for item in values} != set(AblationMode):
        raise AblationConfigurationError("all six ablations must be present once")
    if len(values) != len(set(item.mode for item in values)):
        raise AblationConfigurationError("ablation modes must be unique")
    return values


def ablation_is_safe(spec: AblationSpec) -> bool:
    return all(
        (
            spec.safety_enabled,
            spec.permission_gate_enabled,
            spec.confirmation_gate_enabled,
            spec.idempotency_enabled,
            spec.memory_isolation_enabled,
            spec.calendar_operation_key_enabled,
            spec.plan_cas_enabled,
        )
    )
