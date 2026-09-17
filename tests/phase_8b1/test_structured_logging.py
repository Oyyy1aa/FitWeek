"""Stable JSON logs and recursive redaction."""

import io
import json
import logging

import pytest
from pydantic import BaseModel

from app.observability.logging import (
    LOG_SCHEMA_FIELDS,
    InMemoryStructuredLogSink,
    JsonFormatter,
    configure_logging,
    stable_log_payload,
)
from app.observability.redaction import REDACTED, redact, redact_text, redact_url

pytestmark = pytest.mark.phase_8b1


class SecretPayload(BaseModel):
    api_key: str
    safe: str


def test_stable_log_schema_has_required_fields() -> None:
    payload = stable_log_payload({"event_name": "event", "outcome": "OK"})
    assert tuple(payload) == LOG_SCHEMA_FIELDS
    assert payload["event_name"] == "event"


def test_header_redaction_is_case_insensitive() -> None:
    safe = redact({"Authorization": "Bearer hidden"})
    assert safe == {"Authorization": REDACTED}


def test_nested_dictionary_redaction() -> None:
    safe = redact({"outer": {"password": "hidden", "value": "visible"}})
    assert safe == {"outer": {"password": REDACTED, "value": "visible"}}


def test_list_redaction() -> None:
    safe = redact([{"api_key": "one"}, {"refresh_token": "two"}])
    assert safe == [{"api_key": REDACTED}, {"refresh_token": REDACTED}]


def test_pydantic_model_redaction() -> None:
    safe = redact(SecretPayload(api_key="hidden", safe="visible"))
    assert safe == {"api_key": REDACTED, "safe": "visible"}


def test_exception_redaction_keeps_type_only() -> None:
    safe = redact(RuntimeError("database password=hidden"))
    assert safe == {"exception_type": "RuntimeError"}


def test_url_query_and_credentials_are_removed() -> None:
    safe = redact_url("https://person:secret@example.test/path?token=hidden#part")
    assert safe == "https://example.test/path"


def test_bearer_value_is_removed_without_prefix_or_suffix() -> None:
    safe = redact_text("Authorization: Bearer abc.def.ghi")
    assert "Bearer" not in safe
    assert "abc" not in safe
    assert REDACTED in safe


def test_secret_assignment_is_removed() -> None:
    safe = redact_text("api_key=super-secret continue")
    assert "super-secret" not in safe
    assert safe.startswith(REDACTED)


def test_calendar_and_memory_content_keys_are_redacted() -> None:
    safe = redact({"calendar_event_body": "private", "memory_value": "private too"})
    assert safe == {
        "calendar_event_body": REDACTED,
        "memory_value": REDACTED,
    }


def test_log_formatter_emits_one_valid_json_line() -> None:
    record = logging.LogRecord(
        "fitweek.test", logging.INFO, __file__, 1, "event", (), None
    )
    rendered = JsonFormatter(service="fitweek", environment="test").format(record)
    assert "\n" not in rendered
    parsed = json.loads(rendered)
    assert parsed["event_name"] == "event"
    assert parsed["service"] == "fitweek"


def test_formal_formatter_does_not_emit_traceback() -> None:
    record = logging.LogRecord(
        "fitweek.test",
        logging.ERROR,
        __file__,
        1,
        "safe_error",
        (),
        (RuntimeError, RuntimeError("private"), None),
    )
    rendered = JsonFormatter(service="fitweek", environment="test").format(record)
    assert "Traceback" not in rendered
    assert "private" not in rendered


def test_in_memory_sink_is_bounded() -> None:
    sink = InMemoryStructuredLogSink(limit=2)
    for value in range(3):
        sink.emit({"event_name": f"event-{value}", "level": "INFO"})
    assert [item["event_name"] for item in sink.items()] == ["event-1", "event-2"]


def test_configured_logging_is_structured() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", environment="test", stream=stream)
    logging.getLogger("fitweek.contract").info("configured_event")
    parsed = json.loads(stream.getvalue().strip())
    assert parsed["event_name"] == "configured_event"
    assert (
        tuple(field for field in LOG_SCHEMA_FIELDS if field in parsed)
        == LOG_SCHEMA_FIELDS
    )


def test_stable_payload_drops_unregistered_fields() -> None:
    payload = stable_log_payload(
        {"event_name": "event", "tool_request": "private", "unknown": "value"}
    )
    assert "tool_request" not in payload
    assert "unknown" not in payload
