"""Atomic process-local storage for Recovery Draft artifact bundles."""

from copy import deepcopy
from uuid import UUID

from app.domain.behavior.models import BehaviorMemoryProposal, BehaviorSummary
from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.recovery.models import (
    RecoveryActionCandidateSet,
    RecoveryChangeImpactSnapshot,
    RecoveryDraft,
    RecoveryTrace,
)
from app.persistence.memory.store import InMemoryStore


class InMemoryRecoveryDraftRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get_draft(self, user_id: UUID, draft_id: UUID) -> RecoveryDraft | None:
        async with self._store.lock:
            value = self._store._recovery_drafts.get(draft_id)
            return deepcopy(value) if value and value.user_id == user_id else None

    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> RecoveryDraft | None:
        async with self._store.lock:
            draft_id = self._store._recovery_draft_id_by_request.get(
                (user_id, client_request_id)
            )
            return (
                deepcopy(self._store._recovery_drafts.get(draft_id))
                if draft_id is not None
                else None
            )

    async def save_bundle(
        self,
        *,
        draft: RecoveryDraft,
        behavior_summary: BehaviorSummary,
        impact: RecoveryChangeImpactSnapshot,
        candidate_set: RecoveryActionCandidateSet,
        proposals: tuple[BehaviorMemoryProposal, ...],
        trace: RecoveryTrace,
    ) -> RecoveryDraft:
        key = (draft.user_id, draft.client_request_id)
        owners = (
            behavior_summary.user_id,
            impact.user_id,
            candidate_set.user_id,
            trace.user_id,
            *(item.user_id for item in proposals),
        )
        if any(owner != draft.user_id for owner in owners):
            raise RepositoryConflictError(
                "recovery_bundle.user",
                draft.id,
                expected_version=draft.version,
                actual_version=draft.version,
            )
        async with self._store.lock:
            if draft.id in self._store._recovery_drafts or (
                key in self._store._recovery_draft_id_by_request
            ):
                raise RepositoryUniqueError("recovery_draft.user_request", key)
            self._store.require_first_version("RecoveryDraft", draft.id, draft.version)
            self._store._behavior_summaries[behavior_summary.id] = deepcopy(
                behavior_summary
            )
            self._store._recovery_impacts[impact.id] = deepcopy(impact)
            self._store._recovery_candidate_sets[candidate_set.id] = deepcopy(
                candidate_set
            )
            self._store._recovery_traces[trace.id] = deepcopy(trace)
            for proposal in proposals:
                self._store._behavior_memory_proposals[proposal.id] = deepcopy(proposal)
            self._store._recovery_drafts[draft.id] = deepcopy(draft)
            self._store._recovery_draft_id_by_request[key] = draft.id
            return deepcopy(draft)

    async def update_draft(self, draft: RecoveryDraft) -> RecoveryDraft:
        async with self._store.lock:
            current = self._store._recovery_drafts.get(draft.id)
            if current is None or current.user_id != draft.user_id:
                raise RepositoryUniqueError("recovery_draft.id", draft.id)
            self._store.require_next_version(
                "RecoveryDraft",
                draft.id,
                current_version=current.version,
                incoming_version=draft.version,
            )
            self._store._recovery_drafts[draft.id] = deepcopy(draft)
            return deepcopy(draft)

    async def get_summary(
        self, user_id: UUID, draft_id: UUID
    ) -> BehaviorSummary | None:
        async with self._store.lock:
            draft = self._store._recovery_drafts.get(draft_id)
            if draft is None or draft.user_id != user_id:
                return None
            return deepcopy(
                self._store._behavior_summaries.get(draft.behavior_summary_id)
            )

    async def get_impact(
        self, user_id: UUID, draft_id: UUID
    ) -> RecoveryChangeImpactSnapshot | None:
        async with self._store.lock:
            draft = self._store._recovery_drafts.get(draft_id)
            if draft is None or draft.user_id != user_id:
                return None
            return deepcopy(
                self._store._recovery_impacts.get(draft.change_impact_snapshot_id)
            )

    async def get_candidate_set(
        self, user_id: UUID, draft_id: UUID
    ) -> RecoveryActionCandidateSet | None:
        async with self._store.lock:
            draft = self._store._recovery_drafts.get(draft_id)
            if draft is None or draft.user_id != user_id:
                return None
            return deepcopy(
                self._store._recovery_candidate_sets.get(draft.candidate_set_id)
            )

    async def get_trace(self, user_id: UUID, draft_id: UUID) -> RecoveryTrace | None:
        async with self._store.lock:
            draft = self._store._recovery_drafts.get(draft_id)
            if draft is None or draft.user_id != user_id:
                return None
            value = next(
                (
                    item
                    for item in self._store._recovery_traces.values()
                    if item.draft_id == draft_id and item.user_id == user_id
                ),
                None,
            )
            return deepcopy(value)

    async def list_proposals(
        self, user_id: UUID, draft_id: UUID
    ) -> tuple[BehaviorMemoryProposal, ...]:
        async with self._store.lock:
            draft = self._store._recovery_drafts.get(draft_id)
            if draft is None or draft.user_id != user_id:
                return ()
            values = (
                self._store._behavior_memory_proposals[item]
                for item in draft.behavior_memory_proposal_ids
                if item in self._store._behavior_memory_proposals
            )
            return tuple(deepcopy(item) for item in values)

    async def clear(self) -> None:
        async with self._store.lock:
            self._store._recovery_drafts.clear()
            self._store._recovery_draft_id_by_request.clear()
            self._store._behavior_summaries.clear()
            self._store._recovery_impacts.clear()
            self._store._recovery_candidate_sets.clear()
            self._store._recovery_traces.clear()
            self._store._behavior_memory_proposals.clear()
