"""Persistence port for atomic Schedule Draft application."""

from typing import Protocol
from uuid import UUID

from app.domain.plans.models import WeeklyPlan
from app.domain.schedule_application.models import (
    ScheduleApplicationCommit,
    ScheduleApplicationResult,
)
from app.domain.scheduling.models import ScheduleDraft


class ScheduleApplicationRepository(Protocol):
    async def get_result(
        self, user_id: UUID, result_id: UUID
    ) -> ScheduleApplicationResult | None: ...

    async def get_result_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> ScheduleApplicationResult | None: ...

    async def get_result_by_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> ScheduleApplicationResult | None: ...

    async def commit(
        self,
        *,
        source: WeeklyPlan,
        expected_draft: ScheduleDraft,
        applied_draft: ScheduleDraft,
        revision: WeeklyPlan,
        result: ScheduleApplicationResult,
    ) -> ScheduleApplicationCommit: ...
