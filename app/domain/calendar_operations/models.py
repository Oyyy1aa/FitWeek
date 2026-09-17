"""Immutable Calendar operation drafts, bindings, payloads, and attempts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from app.domain.calendar_operations.enums import (
    CalendarAttemptOutcome,
    CalendarBindingStatus,
    CalendarOperationDraftStatus,
    CalendarOperationItemStatus,
    CalendarOperationType,
)
from app.domain.common import (
    DomainValidationError,
    require_non_blank,
    require_utc_datetime,
    require_version,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarEventPayload:
    session_id: UUID
    stable_uid: str
    summary: str
    description: str
    start: datetime
    end: datetime
    timezone: str
    transparency: str
    payload_fingerprint: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.stable_uid, "stable_uid"),
            (self.summary, "summary"),
            (self.timezone, "timezone"),
            (self.payload_fingerprint, "payload_fingerprint"),
        ):
            require_non_blank(value, name)
        require_utc_datetime(self.start, "start")
        require_utc_datetime(self.end, "end")
        if self.end <= self.start:
            raise DomainValidationError("Calendar event end must be after start.")
        if self.transparency not in {"OPAQUE", "TRANSPARENT"}:
            raise DomainValidationError("transparency is not supported.")


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarEventBinding:
    id: UUID
    user_id: UUID
    provider: str
    calendar_id: str
    root_plan_id: UUID
    session_id: UUID
    external_event_id: str
    stable_uid: str
    last_payload_fingerprint: str
    status: CalendarBindingStatus
    created_at: datetime
    updated_at: datetime
    version: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.provider, "provider"),
            (self.calendar_id, "calendar_id"),
            (self.external_event_id, "external_event_id"),
            (self.stable_uid, "stable_uid"),
            (self.last_payload_fingerprint, "last_payload_fingerprint"),
        ):
            require_non_blank(value, name)
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.updated_at, "updated_at")
        require_version(self.version)


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarOperationItem:
    id: UUID
    operation_type: CalendarOperationType
    session_id: UUID
    operation_key: str
    payload: CalendarEventPayload | None
    binding_id: UUID | None
    status: CalendarOperationItemStatus
    attempt_count: int = 0
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        require_non_blank(self.operation_key, "operation_key")
        if self.attempt_count < 0:
            raise DomainValidationError("attempt_count cannot be negative.")
        if (
            self.operation_type
            in {
                CalendarOperationType.CREATE,
                CalendarOperationType.UPDATE,
                CalendarOperationType.KEEP,
            }
            and self.payload is None
        ):
            raise DomainValidationError("Calendar operation requires a payload.")
        if (
            self.operation_type
            in {
                CalendarOperationType.UPDATE,
                CalendarOperationType.DELETE,
                CalendarOperationType.KEEP,
            }
            and self.binding_id is None
        ):
            raise DomainValidationError("Calendar operation requires a binding.")


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarOperationDraft:
    id: UUID
    user_id: UUID
    client_request_id: str
    request_fingerprint: str
    provider: str
    calendar_id: str
    root_plan_id: UUID
    revision: int
    plan_version: int
    items: tuple[CalendarOperationItem, ...]
    status: CalendarOperationDraftStatus
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        for value, name in (
            (self.client_request_id, "client_request_id"),
            (self.request_fingerprint, "request_fingerprint"),
            (self.provider, "provider"),
            (self.calendar_id, "calendar_id"),
        ):
            require_non_blank(value, name)
        if self.revision < 1 or self.plan_version < 1:
            raise DomainValidationError("Plan revision and version must be positive.")
        require_utc_datetime(self.created_at, "created_at")
        require_utc_datetime(self.updated_at, "updated_at")
        require_version(self.version)
        if len({item.id for item in self.items}) != len(self.items):
            raise DomainValidationError("Calendar operation item IDs must be unique.")
        if len({item.operation_key for item in self.items}) != len(self.items):
            raise DomainValidationError("Calendar operation keys must be unique.")

    def approve(self, at: datetime) -> CalendarOperationDraft:
        if self.status is not CalendarOperationDraftStatus.PENDING_REVIEW:
            raise DomainValidationError(
                "Only a pending Calendar Draft can be approved."
            )
        require_utc_datetime(at, "approved_at")
        return replace(
            self,
            status=CalendarOperationDraftStatus.APPROVED,
            approved_at=at,
            updated_at=at,
            version=self.version + 1,
        )

    def reject(self, at: datetime) -> CalendarOperationDraft:
        if self.status is not CalendarOperationDraftStatus.PENDING_REVIEW:
            raise DomainValidationError(
                "Only a pending Calendar Draft can be rejected."
            )
        require_utc_datetime(at, "rejected_at")
        return replace(
            self,
            status=CalendarOperationDraftStatus.REJECTED,
            rejected_at=at,
            updated_at=at,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarOperationAttempt:
    id: UUID
    user_id: UUID
    draft_id: UUID
    item_id: UUID
    attempt_no: int
    outcome: CalendarAttemptOutcome
    error_code: str | None
    response_reference_hash: str | None
    started_at: datetime
    finished_at: datetime

    def __post_init__(self) -> None:
        if self.attempt_no < 1:
            raise DomainValidationError("attempt_no must be positive.")
        require_utc_datetime(self.started_at, "started_at")
        require_utc_datetime(self.finished_at, "finished_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarProviderResult:
    external_event_id: str | None
    response_reference_hash: str

    def __post_init__(self) -> None:
        require_non_blank(self.response_reference_hash, "response_reference_hash")
