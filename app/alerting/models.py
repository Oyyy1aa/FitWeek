"""Safe low-cardinality alerting models."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SAFE_ALERT_LABELS = frozenset(
    {"alert_name", "severity", "component", "environment", "outcome"}
)
FORBIDDEN_ALERT_LABELS = frozenset(
    {
        "user_id",
        "request_id",
        "correlation_id",
        "run_id",
        "step_id",
        "plan_id",
        "session_id",
        "memory_id",
        "calendar_id",
        "external_event_id",
        "operation_key",
    }
)


class AlertStatus(StrEnum):
    INACTIVE = "INACTIVE"
    PENDING = "PENDING"
    FIRING = "FIRING"
    RESOLVED = "RESOLVED"


class AlertSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class AlertRuleDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    alert_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_]+$")
    severity: AlertSeverity
    component: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    summary: str = Field(min_length=1, max_length=200)
    safe_description: str = Field(min_length=1, max_length=500)
    runbook_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9-]+$")
    for_seconds: int = Field(ge=0, le=86400)


class AlertInstance(BaseModel):
    model_config = ConfigDict(frozen=True)

    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    rule: AlertRuleDefinition
    labels: tuple[tuple[str, str], ...]
    status: AlertStatus
    active_since: datetime | None = None
    updated_at: datetime
    resolved_at: datetime | None = None


class AlertNotification(BaseModel):
    model_config = ConfigDict(frozen=True)

    alert_name: str
    status: AlertStatus
    severity: AlertSeverity
    component: str
    summary: str
    safe_description: str
    runbook_id: str
    started_at: datetime
    resolved_at: datetime | None
    routing_receiver: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class SafeLabelMatcher(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    value: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_label(self) -> Self:
        if self.label not in SAFE_ALERT_LABELS or self.label in FORBIDDEN_ALERT_LABELS:
            raise ValueError("Silence matcher label is not allowed.")
        return self


class AlertSilence(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    matcher: tuple[SafeLabelMatcher, ...]
    starts_at: datetime
    ends_at: datetime
    reason_code: str = Field(pattern=r"^[A-Z0-9_]{3,64}$")
    created_by: str = Field(pattern=r"^(OPERATOR|TEST_HARNESS)$")

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("Silence timestamps must be timezone-aware.")
        if self.ends_at <= self.starts_at:
            raise ValueError("Silence must expire after it starts.")
        if self.ends_at - self.starts_at > timedelta(hours=24):
            raise ValueError("Silence duration cannot exceed 24 hours.")
        if not self.matcher:
            raise ValueError("Silence requires at least one safe matcher.")
        return self


def alert_fingerprint(
    rule: AlertRuleDefinition, labels: tuple[tuple[str, str], ...]
) -> str:
    safe_labels = tuple(sorted(labels))
    if any(name not in SAFE_ALERT_LABELS for name, _ in safe_labels):
        raise ValueError("Alert fingerprint contains a non-low-cardinality label.")
    raw = json.dumps(
        {
            "alert_name": rule.alert_name,
            "severity": rule.severity.value,
            "component": rule.component,
            "labels": safe_labels,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()
