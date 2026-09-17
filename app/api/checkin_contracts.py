"""HTTP DTOs for immutable check-ins and deterministic progress summaries."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.application.checkins import CreateCheckInCommand
from app.application.progress import PlanProgressSummary
from app.domain.checkins.models import CheckInStatus, WorkoutCheckIn


class CheckInCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_event_id: str = Field(min_length=1, max_length=128)
    status: CheckInStatus
    actual_minutes: int | None = Field(default=None, ge=0, le=120)
    perceived_effort: int | None = Field(default=None, ge=1, le=10)
    note: str | None = Field(default=None, max_length=500)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)

    def to_command(self) -> CreateCheckInCommand:
        return CreateCheckInCommand(
            client_event_id=self.client_event_id,
            status=self.status,
            actual_minutes=self.actual_minutes,
            perceived_effort=self.perceived_effort,
            note=self.note,
            occurred_at=self.occurred_at,
        )


class CheckInResponse(BaseModel):
    id: UUID
    client_event_id: str
    user_id: UUID
    plan_id: UUID
    plan_revision: int
    session_id: UUID
    status: CheckInStatus
    actual_minutes: int | None
    perceived_effort: int | None
    note: str | None
    occurred_at: datetime
    created_at: datetime
    updated_at: datetime
    version: int

    @classmethod
    def from_domain(cls, value: WorkoutCheckIn) -> Self:
        return cls(**{field: getattr(value, field) for field in cls.model_fields})


class PlanProgressResponse(BaseModel):
    plan_id: UUID
    plan_revision: int
    planned_sessions: int
    completed_sessions: int
    partially_completed_sessions: int
    skipped_sessions: int
    pending_sessions: int
    planned_minutes: int
    actual_minutes: int
    completion_rate: Decimal
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: PlanProgressSummary) -> Self:
        return cls(**{field: getattr(value, field) for field in cls.model_fields})
