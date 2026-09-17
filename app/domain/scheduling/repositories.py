"""Persistence-independent Schedule Draft repository port."""

from typing import Protocol
from uuid import UUID

from app.domain.scheduling.models import (
    AvailabilityWindow,
    BusySnapshot,
    ScheduleDraft,
    ScheduleTrace,
    TimeSlotCandidateSet,
)


class ScheduleDraftRepository(Protocol):
    async def get_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> ScheduleDraft | None: ...
    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> ScheduleDraft | None: ...
    async def save(
        self,
        draft: ScheduleDraft,
        busy_snapshot: BusySnapshot,
        candidate_set: TimeSlotCandidateSet,
        trace: ScheduleTrace,
        availability_windows: tuple[AvailabilityWindow, ...] = (),
    ) -> ScheduleDraft: ...
    async def update(self, draft: ScheduleDraft) -> ScheduleDraft: ...
    async def get_busy_snapshot(
        self, user_id: UUID, draft_id: UUID
    ) -> BusySnapshot | None: ...
    async def get_candidate_set(
        self, user_id: UUID, draft_id: UUID
    ) -> TimeSlotCandidateSet | None: ...
    async def get_trace(
        self, user_id: UUID, draft_id: UUID
    ) -> ScheduleTrace | None: ...
