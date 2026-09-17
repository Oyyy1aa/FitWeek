"""Consolidated reliability guidance and safe notification schema."""

import json
from pathlib import Path

import pytest

from app.alerting.models import AlertNotification

pytestmark = pytest.mark.phase_8b2

PLAYBOOK_HEADINGS = (
    "### Agent success and idempotency",
    "### Model provider and schema failures",
    "### Worker backlog and expired leases",
    "### Calendar write failures and tool circuits",
    "### Memory safety and degradation",
    "### Unauthorized tool calls",
    "### Observability exporter failures",
)


@pytest.mark.parametrize("heading", PLAYBOOK_HEADINGS)
def test_consolidated_reliability_doc_has_each_playbook(
    project_root: Path, heading: str
) -> None:
    text = (project_root / "docs/reliability.md").read_text(encoding="utf-8")
    assert heading in text
    section = text.split(heading, maxsplit=1)[1].split("\n### ", maxsplit=1)[0]
    assert "**Signals:**" in section
    assert "**Diagnose:**" in section
    assert "**Recovery:**" in section
    assert "**Do not:**" in section
    assert "**Limit:**" in section


def test_notification_schema_is_closed_and_complete(project_root: Path) -> None:
    schema = json.loads(
        (project_root / "observability/alerting/notification-schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(AlertNotification.model_fields)


def test_notification_schema_has_no_sensitive_fields(project_root: Path) -> None:
    raw = (
        (project_root / "observability/alerting/notification-schema.json")
        .read_text(encoding="utf-8")
        .casefold()
    )
    forbidden = {
        "user_id",
        "email",
        "request_id",
        "run_id",
        "plan_id",
        "session_id",
        "memory_value",
        "calendar_event",
        "external_event_id",
        "operation_key",
        "raw_error",
        "stack_trace",
    }
    assert not any(item in raw for item in forbidden)


def test_unauthorized_tool_runbook_preserves_permission_gate(
    project_root: Path,
) -> None:
    text = (project_root / "docs/reliability.md").read_text(encoding="utf-8")
    assert "Do not" in text
    assert "disable permission gates" in text
    assert "adapter invocation" in text


def test_calendar_runbook_forbids_replaying_successful_items(
    project_root: Path,
) -> None:
    text = (project_root / "docs/reliability.md").read_text(encoding="utf-8")
    assert "Do not" in text
    assert "replay every successful Item" in text


def test_routing_policy_only_uses_local_sink_types(project_root: Path) -> None:
    text = (project_root / "observability/alerting/routing-policy.yaml").read_text(
        encoding="utf-8"
    )
    assert "webhook" not in text.casefold()
    assert "slack" not in text.casefold()
    assert "pagerduty" not in text.casefold()
    assert "maximum_attempts: 1" in text


def test_no_public_silence_api_exists(project_root: Path) -> None:
    api_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (project_root / "app/api").rglob("*.py")
    )
    assert '"/silences"' not in api_text
