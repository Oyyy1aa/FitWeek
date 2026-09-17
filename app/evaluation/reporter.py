"""Safe, machine-readable result writer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.evaluation.redaction import find_forbidden_paths


class EvaluationReportError(ValueError):
    pass


def _payload(value: BaseModel | dict[str, Any] | list[object]) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def render_json(value: BaseModel | dict[str, Any] | list[object]) -> str:
    payload = _payload(value)
    if find_forbidden_paths(payload):
        raise EvaluationReportError("result contains a forbidden field")
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def write_json(path: Path, value: BaseModel | dict[str, Any] | list[object]) -> str:
    rendered = render_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8", newline="\n")
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
