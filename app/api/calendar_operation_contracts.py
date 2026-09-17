"""HTTP DTOs for controlled Calendar operation review and execution."""

import hashlib
from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.application.calendar_operations import CreateCalendarOperationCommand
from app.domain.calendar_operations.enums import (
    CalendarAttemptOutcome,
    CalendarBindingStatus,
    CalendarOperationDraftStatus,
    CalendarOperationItemStatus,
    CalendarOperationType,
)
from app.domain.calendar_operations.models import (
    CalendarEventBinding,
    CalendarEventPayload,
    CalendarOperationAttempt,
    CalendarOperationDraft,
    CalendarOperationItem,
)


class CreateCalendarOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=128)
    expected_plan_version: int = Field(ge=1)
    provider: str = Field(min_length=1, max_length=64)
    calendar_id: str = Field(min_length=1, max_length=128)

    def to_command(self) -> CreateCalendarOperationCommand:
        return CreateCalendarOperationCommand(**self.model_dump())


class ReviewCalendarOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class CalendarEventPayloadResponse(BaseModel):
    session_id: UUID
    stable_uid: str
    summary: str
    description: str
    start: datetime
    end: datetime
    timezone: str
    transparency: str
    payload_fingerprint: str

    @classmethod
    def from_domain(cls, value: CalendarEventPayload) -> Self:
        return cls(**{name: getattr(value, name) for name in cls.model_fields})


class CalendarOperationItemResponse(BaseModel):
    id: UUID
    operation_type: CalendarOperationType
    session_id: UUID
    operation_key: str
    payload: CalendarEventPayloadResponse | None
    binding_id: UUID | None
    status: CalendarOperationItemStatus
    attempt_count: int
    last_error_code: str | None

    @classmethod
    def from_domain(cls, value: CalendarOperationItem) -> Self:
        return cls(
            id=value.id,
            operation_type=value.operation_type,
            session_id=value.session_id,
            operation_key=value.operation_key,
            payload=(
                CalendarEventPayloadResponse.from_domain(value.payload)
                if value.payload
                else None
            ),
            binding_id=value.binding_id,
            status=value.status,
            attempt_count=value.attempt_count,
            last_error_code=value.last_error_code,
        )


class CalendarOperationDraftResponse(BaseModel):
    id: UUID
    client_request_id: str
    request_fingerprint: str
    provider: str
    calendar_id: str
    root_plan_id: UUID
    revision: int
    plan_version: int
    items: tuple[CalendarOperationItemResponse, ...]
    status: CalendarOperationDraftStatus
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    rejected_at: datetime | None
    version: int

    @classmethod
    def from_domain(cls, value: CalendarOperationDraft) -> Self:
        return cls(
            **{
                name: getattr(value, name)
                for name in cls.model_fields
                if name != "items"
            },
            items=tuple(
                CalendarOperationItemResponse.from_domain(item) for item in value.items
            ),
        )


class CalendarOperationAttemptResponse(BaseModel):
    id: UUID
    draft_id: UUID
    item_id: UUID
    attempt_no: int
    outcome: CalendarAttemptOutcome
    error_code: str | None
    response_reference_hash: str | None
    started_at: datetime
    finished_at: datetime

    @classmethod
    def from_domain(cls, value: CalendarOperationAttempt) -> Self:
        return cls(**{name: getattr(value, name) for name in cls.model_fields})


class CalendarBindingResponse(BaseModel):
    id: UUID
    provider: str
    calendar_id: str
    root_plan_id: UUID
    session_id: UUID
    external_event_reference_hash: str
    stable_uid: str
    last_payload_fingerprint: str
    status: CalendarBindingStatus
    version: int

    @classmethod
    def from_domain(cls, value: CalendarEventBinding) -> Self:
        return cls(
            id=value.id,
            provider=value.provider,
            calendar_id=value.calendar_id,
            root_plan_id=value.root_plan_id,
            session_id=value.session_id,
            external_event_reference_hash=hashlib.sha256(
                value.external_event_id.encode()
            ).hexdigest(),
            stable_uid=value.stable_uid,
            last_payload_fingerprint=value.last_payload_fingerprint,
            status=value.status,
            version=value.version,
        )
