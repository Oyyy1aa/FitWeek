"""Small defensive redaction helpers for errors, summaries, and traces."""

import hashlib
import re

from app.domain.model_gateway.models import ModelRequest

_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
_CREDENTIAL_URL_PATTERN = re.compile(r"(?i)(https?://)[^/@\s]+@")
_KEY_PATTERN = re.compile(
    r"(?i)(api[-_ ]?key|authorization|secret|token|password)\s*[:=]\s*[^\s,;]+"
)


def fingerprint_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def redact_text(value: str, *, max_length: int = 240) -> str:
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _CREDENTIAL_URL_PATTERN.sub(r"\1[REDACTED]@", redacted)
    redacted = _KEY_PATTERN.sub(r"\1=[REDACTED]", redacted)
    return redacted[:max_length]


def summarize_request(request: ModelRequest) -> dict[str, object]:
    """Return a prompt-free diagnostic summary suitable for a fake provider."""

    return {
        "request_id": str(request.request_id),
        "model": request.model,
        "schema": request.response_schema_name,
        "system_prompt_chars": len(request.system_prompt),
        "user_prompt_chars": len(request.user_prompt),
        "input_fingerprint": fingerprint_text(request.user_prompt),
    }
