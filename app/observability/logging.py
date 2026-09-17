"""Stable single-line JSON logging with centralized redaction."""

from __future__ import annotations

import json
import logging
import sys
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol, TextIO

from app.observability.redaction import redact

LOG_SCHEMA_FIELDS = (
    "timestamp",
    "level",
    "event_name",
    "component",
    "operation",
    "outcome",
    "correlation_id",
    "trace_id",
    "span_id",
    "run_id",
    "step_id",
    "error_category",
    "error_code",
    "degradation_mode",
    "duration_ms",
)


def stable_log_payload(values: Mapping[str, object]) -> dict[str, object]:
    safe = redact(dict(values))
    assert isinstance(safe, dict)
    return {field: safe.get(field) for field in LOG_SCHEMA_FIELDS}


class StructuredLogSink(Protocol):
    def emit(self, payload: Mapping[str, object]) -> None: ...


class JsonFormatter(logging.Formatter):
    """Format existing application records using the same safe schema."""

    def __init__(self, *, service: str, environment: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        values = {field: getattr(record, field, None) for field in LOG_SCHEMA_FIELDS}
        values.update(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "event_name": getattr(record, "event_name", record.getMessage()),
                "component": getattr(record, "component", record.name),
                "operation": getattr(record, "operation", record.getMessage()),
                "outcome": getattr(record, "outcome", "UNKNOWN"),
            }
        )
        payload = stable_log_payload(values)
        payload["service"] = self.service
        payload["environment"] = self.environment
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class JsonStructuredLogSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("fitweek.observability")

    def emit(self, payload: Mapping[str, object]) -> None:
        safe = stable_log_payload(payload)
        level_name = str(safe.get("level") or "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        self._logger.log(
            level,
            str(safe.get("event_name") or "observability_event"),
            extra=safe,
        )


class InMemoryStructuredLogSink:
    def __init__(self, *, limit: int = 1000) -> None:
        self._items: deque[dict[str, object]] = deque(maxlen=limit)

    def emit(self, payload: Mapping[str, object]) -> None:
        self._items.append(stable_log_payload(payload))

    def items(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self._items)


def configure_logging(
    *,
    level: str,
    environment: str,
    structured_enabled: bool = True,
    stream: TextIO | None = None,
) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    if structured_enabled:
        handler.setFormatter(
            JsonFormatter(service="fitweek-api", environment=environment)
        )
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))

    resolved_level = getattr(logging, level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(resolved_level)

    for logger_name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.addHandler(handler)
        uvicorn_logger.setLevel(resolved_level)
        uvicorn_logger.propagate = False
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
