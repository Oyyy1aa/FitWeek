"""Persistence port for atomic in-memory Recovery application commits."""

from typing import Protocol
from uuid import UUID

from app.domain.plans.models import WeeklyPlan
from app.domain.recovery.models import RecoveryDraft
from app.domain.recovery_application.models import (
    RecoveryApplicationCommit,
    RecoveryApplicationResult,
    RecoveryMemoryProposalImportResult,
)
from app.domain.scheduling.models import ScheduleDraft
from app.domain.session_design.models import SessionDesignDraft


class RecoveryApplicationRepository(Protocol):
    async def get_result(
        self, user_id: UUID, result_id: UUID
    ) -> RecoveryApplicationResult | None: ...
    async def get_result_by_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> RecoveryApplicationResult | None: ...
    async def get_result_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> RecoveryApplicationResult | None: ...
    async def commit(
        self,
        *,
        source: WeeklyPlan,
        expected_draft: RecoveryDraft,
        applied_draft: RecoveryDraft,
        revision: WeeklyPlan | None,
        result: RecoveryApplicationResult,
        applied_session_design_drafts: tuple[SessionDesignDraft, ...],
        applied_schedule_drafts: tuple[ScheduleDraft, ...],
    ) -> RecoveryApplicationCommit: ...
    async def get_memory_import(
        self, user_id: UUID, client_request_id: str
    ) -> RecoveryMemoryProposalImportResult | None: ...
    async def save_memory_import(
        self, result: RecoveryMemoryProposalImportResult
    ) -> RecoveryMemoryProposalImportResult: ...
    async def bind_session_design_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID, child_draft_id: UUID
    ) -> None: ...
    async def get_session_design_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID
    ) -> UUID | None: ...
    async def bind_schedule_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID, child_draft_id: UUID
    ) -> None: ...
    async def get_schedule_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID
    ) -> UUID | None: ...
