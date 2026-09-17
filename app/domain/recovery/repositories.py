"""Persistence ports for immutable Recovery artifacts and Draft reviews."""

from typing import Protocol
from uuid import UUID

from app.domain.behavior.models import BehaviorMemoryProposal, BehaviorSummary
from app.domain.recovery.models import (
    RecoveryActionCandidateSet,
    RecoveryChangeImpactSnapshot,
    RecoveryDraft,
    RecoveryTrace,
)


class RecoveryDraftRepository(Protocol):
    async def get_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> RecoveryDraft | None: ...
    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> RecoveryDraft | None: ...
    async def save_bundle(
        self,
        *,
        draft: RecoveryDraft,
        behavior_summary: BehaviorSummary,
        impact: RecoveryChangeImpactSnapshot,
        candidate_set: RecoveryActionCandidateSet,
        proposals: tuple[BehaviorMemoryProposal, ...],
        trace: RecoveryTrace,
    ) -> RecoveryDraft: ...
    async def update_draft(self, draft: RecoveryDraft) -> RecoveryDraft: ...
    async def get_summary(
        self, user_id: UUID, draft_id: UUID
    ) -> BehaviorSummary | None: ...
    async def get_impact(
        self, user_id: UUID, draft_id: UUID
    ) -> RecoveryChangeImpactSnapshot | None: ...
    async def get_candidate_set(
        self, user_id: UUID, draft_id: UUID
    ) -> RecoveryActionCandidateSet | None: ...
    async def get_trace(
        self, user_id: UUID, draft_id: UUID
    ) -> RecoveryTrace | None: ...
    async def list_proposals(
        self, user_id: UUID, draft_id: UUID
    ) -> tuple[BehaviorMemoryProposal, ...]: ...
