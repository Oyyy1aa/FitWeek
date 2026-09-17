"""Concurrency-safe process-local Schedule Draft adapter."""

from copy import deepcopy
from uuid import UUID

from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.scheduling.models import (
    AvailabilityWindow,
    BusySnapshot,
    ScheduleDraft,
    ScheduleTrace,
    TimeSlotCandidateSet,
)
from app.persistence.memory.store import InMemoryStore


class InMemoryScheduleDraftRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get_draft(self, user_id: UUID, draft_id: UUID) -> ScheduleDraft | None:
        async with self._store.lock:
            value = self._store._schedule_drafts.get(draft_id)
            return (
                deepcopy(value)
                if value is not None and value.user_id == user_id
                else None
            )

    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> ScheduleDraft | None:
        async with self._store.lock:
            draft_id = self._store._schedule_draft_id_by_request.get(
                (user_id, client_request_id)
            )
            return (
                deepcopy(self._store._schedule_drafts.get(draft_id))
                if draft_id
                else None
            )

    async def save(
        self,
        draft: ScheduleDraft,
        busy_snapshot: BusySnapshot,
        candidate_set: TimeSlotCandidateSet,
        trace: ScheduleTrace,
        availability_windows: tuple[AvailabilityWindow, ...] = (),
    ) -> ScheduleDraft:
        del availability_windows
        async with self._store.lock:
            key = (draft.user_id, draft.client_request_id)
            if (
                draft.id in self._store._schedule_drafts
                or key in self._store._schedule_draft_id_by_request
            ):
                raise RepositoryUniqueError("schedule_draft.request", key)
            self._store._busy_snapshots[busy_snapshot.id] = deepcopy(busy_snapshot)
            self._store._time_slot_candidate_sets[candidate_set.id] = deepcopy(
                candidate_set
            )
            self._store._schedule_drafts[draft.id] = deepcopy(draft)
            self._store._schedule_draft_id_by_request[key] = draft.id
            self._store._schedule_traces[draft.id] = deepcopy(trace)
            return deepcopy(draft)

    async def update(self, draft: ScheduleDraft) -> ScheduleDraft:
        async with self._store.lock:
            current = self._store._schedule_drafts.get(draft.id)
            if current is None:
                raise RepositoryUniqueError("schedule_draft.id", draft.id)
            if draft.version != current.version + 1:
                raise RepositoryConflictError(
                    "ScheduleDraft",
                    draft.id,
                    expected_version=current.version + 1,
                    actual_version=draft.version,
                )
            self._store._schedule_drafts[draft.id] = deepcopy(draft)
            return deepcopy(draft)

    async def get_busy_snapshot(
        self, user_id: UUID, draft_id: UUID
    ) -> BusySnapshot | None:
        async with self._store.lock:
            draft = self._store._schedule_drafts.get(draft_id)
            if draft is None or draft.user_id != user_id:
                return None
            return deepcopy(self._store._busy_snapshots.get(draft.busy_snapshot_id))

    async def get_candidate_set(
        self, user_id: UUID, draft_id: UUID
    ) -> TimeSlotCandidateSet | None:
        async with self._store.lock:
            draft = self._store._schedule_drafts.get(draft_id)
            if draft is None or draft.user_id != user_id:
                return None
            return deepcopy(
                self._store._time_slot_candidate_sets.get(draft.candidate_set_id)
            )

    async def get_trace(self, user_id: UUID, draft_id: UUID) -> ScheduleTrace | None:
        async with self._store.lock:
            draft = self._store._schedule_drafts.get(draft_id)
            trace = self._store._schedule_traces.get(draft_id)
            return (
                deepcopy(trace)
                if draft is not None and draft.user_id == user_id
                else None
            )

    async def clear(self) -> None:
        async with self._store.lock:
            self._store._busy_snapshots.clear()
            self._store._time_slot_candidate_sets.clear()
            self._store._schedule_drafts.clear()
            self._store._schedule_draft_id_by_request.clear()
            self._store._schedule_traces.clear()
