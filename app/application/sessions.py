"""Read-only workout-session use cases."""

from uuid import UUID

from app.application.errors import ResourceNotFound
from app.domain.plans.repositories import PlanRepository
from app.domain.sessions.models import WorkoutSession
from app.domain.users.models import UserAccount


class SessionService:
    """Query sessions through the plan aggregate repository contract."""

    def __init__(self, repository: PlanRepository) -> None:
        self._repository = repository

    async def list_plan_sessions(
        self,
        user: UserAccount,
        plan_id: UUID,
    ) -> list[WorkoutSession]:
        plan = await self._repository.get_for_user(plan_id, user.id)
        if plan is None:
            raise ResourceNotFound("Weekly plan was not found.")
        return await self._repository.list_sessions_for_user(plan_id, user.id)

    async def get_session(
        self,
        user: UserAccount,
        session_id: UUID,
    ) -> WorkoutSession:
        session = await self._repository.get_session_for_user(session_id, user.id)
        if session is None:
            raise ResourceNotFound("Workout session was not found.")
        return session
