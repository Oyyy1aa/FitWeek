"""Strict extraction of exactly one top-level JSON object."""

import json
from typing import cast

from app.domain.model_gateway.errors import (
    ModelEmptyResponseError,
    ModelInvalidJsonError,
    ModelResponseTooLargeError,
)


def extract_json_object(raw_text: str, *, max_bytes: int) -> dict[str, object]:
    """Extract one object from raw/fenced/prefixed text without repair or eval."""

    if not raw_text.strip():
        raise ModelEmptyResponseError()
    if len(raw_text.encode("utf-8")) > max_bytes:
        raise ModelResponseTooLargeError()

    spans: list[tuple[int, int]] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, character in enumerate(raw_text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"' and depth > 0:
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}":
            if depth == 0:
                raise ModelInvalidJsonError()
            depth -= 1
            if depth == 0 and start is not None:
                spans.append((start, index + 1))
                start = None
    if depth != 0 or in_string or len(spans) != 1:
        raise ModelInvalidJsonError()
    candidate = raw_text[spans[0][0] : spans[0][1]]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ModelInvalidJsonError() from exc
    if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
        raise ModelInvalidJsonError()
    return cast(dict[str, object], parsed)
