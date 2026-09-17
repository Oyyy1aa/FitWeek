"""Canonical fingerprints and repeat-comparison helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any


def canonicalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(canonicalize(item) for item in value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def stable_fingerprint(value: Any) -> str:
    payload = json.dumps(
        canonicalize(value), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def repetitions_are_identical(values: Sequence[object]) -> bool:
    return bool(values) and len({stable_fingerprint(item) for item in values}) == 1
