"""Unicode-only deterministic Memory normalization and safety validation."""

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping

from app.domain.memory.errors import MemoryInvalidValueError

_SPACE = re.compile(r"\s+")
_PROHIBITED = (
    "medical condition",
    "injury",
    "diagnosis",
    "treatment",
    "pregnancy",
    "chronic disease",
    "受伤",
    "诊断",
    "治疗",
    "孕期",
    "慢性病",
    "疾病",
)


def normalize_display(value: str, *, maximum: int = 240) -> str:
    normalized = _SPACE.sub(" ", unicodedata.normalize("NFKC", value).strip())
    if not normalized or len(normalized) > maximum:
        raise MemoryInvalidValueError(
            f"Memory value must contain between 1 and {maximum} characters."
        )
    lowered = normalized.casefold()
    if any(term in lowered for term in _PROHIBITED):
        raise MemoryInvalidValueError(
            "Medical or health inference cannot be stored as Memory."
        )
    return normalized


def normalize_key(value: str) -> str:
    return normalize_display(value, maximum=80).casefold().replace(" ", "_")


def normalize_value(value: str) -> str:
    return normalize_display(value, maximum=120).casefold()


def fingerprint(payload: Mapping[str, object]) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
