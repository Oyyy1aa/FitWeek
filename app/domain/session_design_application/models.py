"""Immutable Session Design application commands, previews, and audit results."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.common import (
    DomainValidationError,
    require_non_blank,
    require_utc_datetime,
    require_version,
)
from app.domain.sessions.models import SessionExercise, SessionType
from app.safety.models import SafetyValidationResult

APPLICATION_POLICY_VERSION = "session-design-plan-apply-v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplySessionDesignCommand:
    client_request_id: str
    expected_draft_version: int
    root_plan_id: UUID
    source_revision: int
    expected_plan_version: int
    target_session_id: UUID

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        if len(self.client_request_id.strip()) > 128:
            raise DomainValidationError("client_request_id is too long.")
        require_version(self.expected_draft_version)
        require_version(self.expected_plan_version)
        if self.source_revision < 1:
            raise DomainValidationError("source_revision must be positive.")
        object.__setattr__(self, "client_request_id", self.client_request_id.strip())


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionCompositionSummary:
    session_id: UUID
    session_type: SessionType
    session_version: int
    exercise_ids: tuple[str, ...]
    exercises: tuple[SessionExercise, ...]
    calculated_seconds: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDesignApplicationMetadata:
    source_draft_id: UUID
    source_draft_version: int
    source_candidate_set_id: UUID
    source_context_snapshot_reference_id: UUID
    source_revision: int
    target_session_id: UUID
    changed_session_ids: tuple[UUID, ...]
    preserved_session_ids: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    application_fingerprint: str
    policy_version: str = APPLICATION_POLICY_VERSION

    def __post_init__(self) -> None:
        require_version(self.source_draft_version)
        if self.source_revision < 1:
            raise DomainValidationError("source_revision must be positive.")
        require_non_blank(self.application_fingerprint, "application_fingerprint")
        require_non_blank(self.policy_version, "policy_version")
        if self.changed_session_ids != (self.target_session_id,):
            raise DomainValidationError("exactly the target Session must be changed.")
        groups = (
            set(self.changed_session_ids),
            set(self.preserved_session_ids),
            set(self.immutable_session_ids),
        )
        if groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]:
            raise DomainValidationError("Session impact groups must be disjoint.")


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDesignApplyPreview:
    draft_id: UUID
    draft_version: int
    root_plan_id: UUID
    source_revision: int
    source_plan_version: int
    target_session_id: UUID
    target_session_before: SessionCompositionSummary
    target_session_after: SessionCompositionSummary
    preserved_session_ids: tuple[UUID, ...]
    immutable_session_ids: tuple[UUID, ...]
    changed_session_ids: tuple[UUID, ...]
    duration_delta_seconds: int
    available_window_seconds: int
    validation: SafetyValidationResult
    application_fingerprint: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDesignApplicationResult:
    id: UUID
    user_id: UUID
    client_request_id: str
    application_fingerprint: str
    draft_id: UUID
    root_plan_id: UUID
    source_revision: int
    created_revision: int
    target_session_id: UUID
    previous_plan_version: int
    resulting_plan_version: int
    created_at: datetime

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        require_non_blank(self.application_fingerprint, "application_fingerprint")
        if (
            self.source_revision < 1
            or self.created_revision != self.source_revision + 1
        ):
            raise DomainValidationError("created_revision must follow source_revision.")
        require_version(self.previous_plan_version)
        require_version(self.resulting_plan_version)
        require_utc_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDesignApplicationCommit:
    result: SessionDesignApplicationResult
    plan_revision_id: UUID
    draft_id: UUID
    created: bool
