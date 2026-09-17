"""Persistence-independent check-in repository contract."""

from typing import Protocol
from uuid import UUID

from app.domain.checkins.models import WorkoutCheckIn


class CheckInRepository(Protocol):
    async def get(self, check_in_id: UUID) -> WorkoutCheckIn | None: ...

    async def get_by_client_event_id(
        self, user_id: UUID, client_event_id: str
    ) -> WorkoutCheckIn | None: ...

    async def get_by_session_id(self, session_id: UUID) -> WorkoutCheckIn | None: ...

    async def save(self, check_in: WorkoutCheckIn) -> WorkoutCheckIn: ...

    async def list_by_plan(
        self, plan_id: UUID, plan_revision: int
    ) -> list[WorkoutCheckIn]: ...

    async def list_by_series(self, plan_id: UUID) -> list[WorkoutCheckIn]: ...

    async def list_by_user(self, user_id: UUID) -> list[WorkoutCheckIn]: ...
