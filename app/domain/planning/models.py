"""Immutable inputs and outputs for deterministic weekly-plan generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from app.domain.common import DomainValidationError, LocationType, require_datetime
from app.domain.sessions.models import SessionType


def _require_aware(value: datetime, field_name: str) -> None:
    require_datetime(value, field_name)
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(
            f"{field_name} must be timezone-aware.",
            code="NAIVE_DATETIME",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AvailabilitySlot:
    """A user-supplied interval in which one workout may be scheduled."""

    start: datetime
    end: datetime
    location_type: LocationType

    def __post_init__(self) -> None:
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        if self.start >= self.end:
            raise DomainValidationError(
                "availability slot start must be earlier than end.",
                code="INVALID_AVAILABILITY_SLOT",
            )
        if self.end.astimezone(UTC) - self.start.astimezone(UTC) < timedelta(
            minutes=15
        ):
            raise DomainValidationError(
                "availability slots must be at least 15 minutes long.",
                code="NO_VALID_TIME_SLOT",
            )
        if not isinstance(self.location_type, LocationType):
            raise DomainValidationError("location_type must be a LocationType.")

    @property
    def start_utc(self) -> datetime:
        return self.start.astimezone(UTC)

    @property
    def end_utc(self) -> datetime:
        return self.end.astimezone(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerateWeeklyPlanCommand:
    """Validated, persistence-independent generation request."""

    week_start: date
    availability_slots: tuple[AvailabilitySlot, ...]
    preferred_locations: tuple[LocationType, ...] = ()
    preferred_session_types: tuple[SessionType, ...] = ()
    client_request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.week_start, date) or isinstance(
            self.week_start, datetime
        ):
            raise DomainValidationError("week_start must be a date.")
        if self.week_start.weekday() != 0:
            raise DomainValidationError(
                "week_start must be a Monday.", code="INVALID_WEEK_START"
            )
        if not isinstance(self.availability_slots, tuple) or any(
            not isinstance(slot, AvailabilitySlot) for slot in self.availability_slots
        ):
            raise DomainValidationError(
                "availability_slots must be AvailabilitySlot values."
            )
        if not self.availability_slots:
            raise DomainValidationError(
                "at least one availability slot is required.",
                code="INSUFFICIENT_AVAILABILITY",
            )
        if len(self.availability_slots) > 21:
            raise DomainValidationError(
                "no more than 21 availability slots are allowed.",
                code="TOO_MANY_AVAILABILITY_SLOTS",
            )
        if any(
            not isinstance(location, LocationType)
            for location in self.preferred_locations
        ):
            raise DomainValidationError(
                "preferred_locations must contain LocationType values."
            )
        if any(
            not isinstance(session_type, SessionType)
            for session_type in self.preferred_session_types
        ):
            raise DomainValidationError(
                "preferred_session_types must contain SessionType values."
            )
        if self.client_request_id is not None and not self.client_request_id.strip():
            raise DomainValidationError("client_request_id must not be blank.")
        self._validate_week_bounds()
        self._validate_non_overlapping_slots()

    def _validate_week_bounds(self) -> None:
        week_start = datetime.combine(self.week_start, time.min, tzinfo=UTC)
        week_end = week_start + timedelta(days=7)
        for slot in self.availability_slots:
            if slot.start_utc < week_start or slot.end_utc > week_end:
                raise DomainValidationError(
                    "availability slots must be fully contained in the target week.",
                    code="SESSION_OUTSIDE_WEEK",
                )

    def _validate_non_overlapping_slots(self) -> None:
        ordered = sorted(
            self.availability_slots,
            key=lambda slot: (slot.start_utc, slot.end_utc, slot.location_type.value),
        )
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.start_utc < previous.end_utc:
                raise DomainValidationError(
                    "availability slots must not overlap.",
                    code="AVAILABILITY_SLOT_OVERLAP",
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerationMetadata:
    """Safe, reproducible metadata attached to every generated plan."""

    generation_policy_version: str
    catalog_version: str
    input_fingerprint: str
    context_snapshot_reference_id: UUID | None = None
    context_fingerprint: str = "none"
    context_contract_version: str = "none"
    context_policy_version: str = "none"
    context_degradation_state: str = "NONE"
    included_memory_count: int = 0
    shadowed_memory_count: int = 0
    preference_fallbacks: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, str]:
        values = {
            "generation_policy_version": self.generation_policy_version,
            "catalog_version": self.catalog_version,
            "input_fingerprint": self.input_fingerprint,
            "context_snapshot_reference_id": (
                str(self.context_snapshot_reference_id)
                if self.context_snapshot_reference_id is not None
                else "none"
            ),
            "context_fingerprint": self.context_fingerprint,
            "context_contract_version": self.context_contract_version,
            "context_policy_version": self.context_policy_version,
            "context_degradation_state": self.context_degradation_state,
            "included_memory_count": str(self.included_memory_count),
            "shadowed_memory_count": str(self.shadowed_memory_count),
            "preference_fallbacks": ",".join(self.preference_fallbacks) or "none",
        }
        return values

    @classmethod
    def from_dict(cls, values: dict[str, str]) -> GenerationMetadata:
        reference = values.get("context_snapshot_reference_id", "none")
        fallbacks = values.get("preference_fallbacks", "none")
        return cls(
            generation_policy_version=values["generation_policy_version"],
            catalog_version=values["catalog_version"],
            input_fingerprint=values["input_fingerprint"],
            context_snapshot_reference_id=(
                None if reference == "none" else UUID(reference)
            ),
            context_fingerprint=values.get("context_fingerprint", "none"),
            context_contract_version=values.get("context_contract_version", "none"),
            context_policy_version=values.get("context_policy_version", "none"),
            context_degradation_state=values.get("context_degradation_state", "NONE"),
            included_memory_count=int(values.get("included_memory_count", "0")),
            shadowed_memory_count=int(values.get("shadowed_memory_count", "0")),
            preference_fallbacks=(
                () if fallbacks == "none" else tuple(fallbacks.split(","))
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerationFailureReason:
    """A stable client-safe reason why generation could not complete."""

    code: str
    message: str


class PlanGenerationError(RuntimeError):
    """Raised by pure planning components when no safe candidate can be built."""

    def __init__(self, *reasons: GenerationFailureReason) -> None:
        if not reasons:
            reasons = (
                GenerationFailureReason(
                    code="PLAN_GENERATION_FAILED",
                    message="A safe weekly plan could not be generated.",
                ),
            )
        self.reasons = tuple(reasons)
        super().__init__(self.reasons[0].message)
