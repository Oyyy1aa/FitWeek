"""Persistence-independent weekly plan repository contract."""

from typing import Protocol
from uuid import UUID

from app.domain.plans.models import WeeklyPlan
from app.domain.sessions.models import WorkoutSession


class PlanRepository(Protocol):
    async def get(self, plan_id: UUID) -> WeeklyPlan | None: ...

    async def get_for_user(self, plan_id: UUID, user_id: UUID) -> WeeklyPlan | None: ...

    async def save(self, plan: WeeklyPlan) -> WeeklyPlan: ...

    async def list_by_user(self, user_id: UUID) -> list[WeeklyPlan]: ...

    async def list_sessions(self, plan_id: UUID) -> list[WorkoutSession]: ...

    async def list_sessions_for_user(
        self, plan_id: UUID, user_id: UUID
    ) -> list[WorkoutSession]: ...

    async def get_session(self, session_id: UUID) -> WorkoutSession | None: ...

    async def get_session_for_user(
        self, session_id: UUID, user_id: UUID
    ) -> WorkoutSession | None: ...

    async def get_current_plan_for_session(
        self, session_id: UUID, user_id: UUID
    ) -> WeeklyPlan | None: ...

    async def list_revisions_for_user(
        self, series_id: UUID, user_id: UUID
    ) -> list[WeeklyPlan]: ...

    async def get_revision_for_user(
        self, series_id: UUID, user_id: UUID, revision: int
    ) -> WeeklyPlan | None: ...

    async def get_current_confirmed(
        self, series_id: UUID, user_id: UUID
    ) -> WeeklyPlan | None: ...

    async def find_by_replan_request(
        self, user_id: UUID, client_request_id: str
    ) -> WeeklyPlan | None: ...

    async def get_generation_request(
        self, user_id: UUID, client_request_id: str
    ) -> tuple[str, WeeklyPlan] | None: ...

    async def bind_generation_request(
        self,
        user_id: UUID,
        client_request_id: str,
        input_fingerprint: str,
        plan_id: UUID,
    ) -> None: ...
