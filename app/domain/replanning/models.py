"""Immutable commands and results for deterministic local replanning."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.common import (
    DomainValidationError,
    require_non_blank,
    require_utc_datetime,
    require_version,
)
from app.domain.planning.models import AvailabilitySlot


class PlanChangeType(StrEnum):
    AVAILABILITY_CHANGED = "AVAILABILITY_CHANGED"
    EQUIPMENT_CHANGED = "EQUIPMENT_CHANGED"
    SESSION_DURATION_CHANGED = "SESSION_DURATION_CHANGED"
    EXCLUDED_FEATURE_CHANGED = "EXCLUDED_FEATURE_CHANGED"


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalReplanCommand:
    client_request_id: str
    expected_plan_version: int
    change_type: PlanChangeType
    effective_from: datetime
    replacement_availability_slots: tuple[AvailabilitySlot, ...] = ()
    available_equipment: tuple[str, ...] | None = None
    max_session_minutes: int | None = None
    excluded_features: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        if len(self.client_request_id.strip()) > 128:
            raise DomainValidationError(
                "client_request_id must not exceed 128 characters."
            )
        require_version(self.expected_plan_version)
        if not isinstance(self.change_type, PlanChangeType):
            raise DomainValidationError(
                "change_type is not supported for local replanning.",
                code="UNSUPPORTED_LOCAL_CHANGE",
            )
        require_utc_datetime(self.effective_from, "effective_from")
        if len(self.replacement_availability_slots) > 21:
            raise DomainValidationError(
                "no more than 21 replacement slots are allowed."
            )
        if any(
            not isinstance(slot, AvailabilitySlot)
            for slot in self.replacement_availability_slots
        ):
            raise DomainValidationError(
                "replacement slots must be AvailabilitySlot values."
            )
        ordered_slots = sorted(
            self.replacement_availability_slots,
            key=lambda item: (item.start_utc, item.end_utc),
        )
        if any(
            current.start_utc < previous.end_utc
            for previous, current in zip(ordered_slots, ordered_slots[1:], strict=False)
        ):
            raise DomainValidationError(
                "replacement availability slots must not overlap."
            )
        if self.max_session_minutes is not None and (
            isinstance(self.max_session_minutes, bool)
            or not isinstance(self.max_session_minutes, int)
            or not 15 <= self.max_session_minutes <= 60
        ):
            raise DomainValidationError(
                "max_session_minutes must be between 15 and 60."
            )
        normalized_equipment = self._normalize_tokens(
            self.available_equipment, "available_equipment"
        )
        normalized_features = self._normalize_tokens(
            self.excluded_features, "excluded_features"
        )
        object.__setattr__(self, "client_request_id", self.client_request_id.strip())
        object.__setattr__(self, "available_equipment", normalized_equipment)
        object.__setattr__(self, "excluded_features", normalized_features)
        self._require_matching_payload()

    @staticmethod
    def _normalize_tokens(
        values: tuple[str, ...] | None, field_name: str
    ) -> tuple[str, ...] | None:
        if values is None:
            return None
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise DomainValidationError(f"{field_name} must not contain blank values.")
        return tuple(sorted({item.strip().lower() for item in values}))

    def _require_matching_payload(self) -> None:
        populated = {
            PlanChangeType.AVAILABILITY_CHANGED: bool(
                self.replacement_availability_slots
            ),
            PlanChangeType.EQUIPMENT_CHANGED: self.available_equipment is not None,
            PlanChangeType.SESSION_DURATION_CHANGED: self.max_session_minutes
            is not None,
            PlanChangeType.EXCLUDED_FEATURE_CHANGED: self.excluded_features is not None,
        }
        if not populated[self.change_type] or sum(populated.values()) != 1:
            raise DomainValidationError(
                "the request must contain only the payload for its change_type.",
                code="UNSUPPORTED_LOCAL_CHANGE",
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangeImpactReason:
    code: str
    message: str
    session_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangeImpact:
    affected_session_ids: tuple[UUID, ...]
    preserved_session_ids: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    reasons: tuple[ChangeImpactReason, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanChangeMetadata:
    change_type: PlanChangeType
    source_revision: int
    changed_session_ids: tuple[UUID, ...]
    preserved_session_ids: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    change_fingerprint: str
    replanning_policy_version: str
    client_request_id: str

    def __post_init__(self) -> None:
        if self.source_revision < 1:
            raise DomainValidationError("source_revision must be positive.")
        for field_name in (
            "change_fingerprint",
            "replanning_policy_version",
            "client_request_id",
        ):
            require_non_blank(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplanningFailureReason:
    code: str
    message: str
    session_id: UUID | None = None


class LocalReplanningError(RuntimeError):
    def __init__(self, *reasons: ReplanningFailureReason) -> None:
        if not reasons:
            reasons = (
                ReplanningFailureReason(
                    code="LOCAL_REPLANNING_FAILED",
                    message="A safe local revision could not be created.",
                ),
            )
        self.reasons = tuple(reasons)
        super().__init__(self.reasons[0].message)
