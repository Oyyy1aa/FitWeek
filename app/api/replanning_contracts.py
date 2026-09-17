"""HTTP DTOs for local-change commands and revision views."""

from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.contracts import (
    AvailabilitySlotRequest,
    PlanConfirmRequest,
    WeeklyPlanResponse,
)
from app.application.errors import BusinessRuleViolation
from app.domain.replanning.models import LocalReplanCommand, PlanChangeType


class LocalReplanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=128)
    expected_plan_version: int = Field(ge=1)
    change_type: str
    effective_from: datetime
    replacement_availability_slots: list[AvailabilitySlotRequest] = Field(
        default_factory=list, max_length=21
    )
    available_equipment: list[str] | None = None
    max_session_minutes: int | None = Field(default=None, ge=15, le=60)
    excluded_features: list[str] | None = None

    @field_validator("effective_from")
    @classmethod
    def require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("effective_from must be timezone-aware")
        return value.astimezone(UTC)

    def to_command(self) -> LocalReplanCommand:
        try:
            change_type = PlanChangeType(self.change_type)
        except ValueError as exc:
            raise BusinessRuleViolation(
                "The requested local change type is unsupported.",
                code="UNSUPPORTED_LOCAL_CHANGE",
            ) from exc
        return LocalReplanCommand(
            client_request_id=self.client_request_id,
            expected_plan_version=self.expected_plan_version,
            change_type=change_type,
            effective_from=self.effective_from,
            replacement_availability_slots=tuple(
                item.to_domain() for item in self.replacement_availability_slots
            ),
            available_equipment=(
                None
                if self.available_equipment is None
                else tuple(self.available_equipment)
            ),
            max_session_minutes=self.max_session_minutes,
            excluded_features=(
                None
                if self.excluded_features is None
                else tuple(self.excluded_features)
            ),
        )


class LocalReplanResponse(BaseModel):
    plan: WeeklyPlanResponse
    created: bool
    repair_attempts: int


class PlanRevisionResponse(BaseModel):
    plan: WeeklyPlanResponse
    is_current_revision: bool

    @classmethod
    def create(cls, plan: WeeklyPlanResponse, is_current: bool) -> Self:
        return cls(plan=plan, is_current_revision=is_current)


__all__ = [
    "LocalReplanRequest",
    "LocalReplanResponse",
    "PlanConfirmRequest",
    "PlanRevisionResponse",
]
