"""Central recursive redaction for telemetry-safe values."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "oauth_token",
        "access_token",
        "refresh_token",
        "password",
        "private_key",
        "secret",
        "token",
        "user_message",
        "prompt",
        "raw_model_response",
        "model_raw_response",
        "tool_request",
        "tool_response",
        "calendar_event",
        "calendar_event_body",
        "checkin_note",
        "memory_value",
        "operation_key",
        "external_event_id",
    }
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
    r"\s*[:=]\s*[^\s,;]+"
)


def normalize_key(value: object) -> str:
    return str(value).strip().casefold().replace("-", "_")


def is_sensitive_key(value: object) -> bool:
    key = normalize_key(value)
    return key in _SENSITIVE_KEYS or any(
        token in key for token in ("authorization", "private_key", "password")
    )


def redact_url(value: str) -> str:
    """Remove credentials, query, and fragments without retaining secret edges."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return REDACTED
    if not parsed.scheme or not parsed.netloc:
        return value
    host = parsed.hostname or "redacted-host"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def redact_text(value: str) -> str:
    text = _BEARER.sub(REDACTED, value)
    text = _SECRET_ASSIGNMENT.sub(REDACTED, text)
    if "://" in text:
        return redact_url(text)
    return text


def redact(value: object, *, key: object | None = None) -> object:
    """Return a detached recursively redacted telemetry representation."""

    if key is not None and is_sensitive_key(key):
        return REDACTED
    if isinstance(value, BaseException):
        return {"exception_type": type(value).__name__}
    if isinstance(value, BaseModel):
        return redact(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item, key=item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def safe_attributes(
    values: Mapping[str, object], allowed: frozenset[str]
) -> dict[str, str | bool | int | float]:
    result: dict[str, str | bool | int | float] = {}
    for key, value in values.items():
        if key not in allowed or is_sensitive_key(key) or value is None:
            continue
        redacted = redact(value, key=key)
        if isinstance(redacted, (str, bool, int, float)):
            result[key] = redacted
    return result
