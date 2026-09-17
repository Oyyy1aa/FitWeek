"""Closed-field validation for datasets and machine-readable results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

FORBIDDEN_EVALUATION_KEYS = frozenset(
    {
        "name",
        "email",
        "address",
        "phone",
        "password",
        "authorization",
        "api_key",
        "oauth",
        "access_token",
        "refresh_token",
        "private_key",
        "user_message",
        "prompt",
        "raw_model_response",
        "memory_value",
        "checkin_note",
        "calendar_event",
        "traceback",
    }
)


def find_forbidden_paths(value: Any, path: str = "$") -> tuple[str, ...]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            token = str(key).casefold()
            child = f"{path}.{key}"
            if token in FORBIDDEN_EVALUATION_KEYS:
                findings.append(child)
            findings.extend(find_forbidden_paths(item, child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            findings.extend(find_forbidden_paths(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        lowered = value.casefold()
        if "c:\\users\\" in lowered or "/home/" in lowered or "/users/" in lowered:
            findings.append(path)
    return tuple(findings)
