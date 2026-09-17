"""In-memory weekly-plan and workout-session repository."""

from copy import deepcopy
from uuid import UUID

from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.replanning.models import PlanChangeMetadata
from app.domain.sessions.models import WorkoutSession
from app.persistence.memory.store import (
    InMemoryStore,
    RepositoryConflictError,
    RepositoryUniqueError,
    StoredSession,
)


class InMemoryPlanRepository:
    """Persist plan aggregates and rebuild their read-only session index."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get(self, plan_id: UUID) -> WeeklyPlan | None:
        async with self._store.lock:
            return deepcopy(self._store._plans.get(plan_id))

    async def get_for_user(self, plan_id: UUID, user_id: UUID) -> WeeklyPlan | None:
        """Return a plan only when it belongs to the requested user."""

        async with self._store.lock:
            plan = self._store._plans.get(plan_id)
            if plan is None or plan.user_id != user_id:
                return None
            return deepcopy(plan)

    async def save(self, plan: WeeklyPlan) -> WeeklyPlan:
        snapshot = deepcopy(plan)
        key = (snapshot.user_id, snapshot.week_start, snapshot.revision)
        async with self._store.lock:
            existing = self._store._plans.get(snapshot.id)
            owner_plan_id = self._store._plan_id_by_revision.get(key)
            if existing is None:
                self._store.require_first_version(
                    "weekly_plan", snapshot.id, snapshot.version
                )
                if owner_plan_id is not None:
                    raise RepositoryUniqueError("weekly_plan.user_week_revision", key)
            else:
                existing_key = (
                    existing.user_id,
                    existing.week_start,
                    existing.revision,
                )
                if existing_key != key:
                    raise RepositoryConflictError(
                        "weekly_plan.identity",
                        snapshot.id,
                        expected_version=existing.version + 1,
                        actual_version=snapshot.version,
                    )
                self._store.require_next_version(
                    "weekly_plan",
                    snapshot.id,
                    current_version=existing.version,
                    incoming_version=snapshot.version,
                )
                if owner_plan_id not in {None, snapshot.id}:
                    raise RepositoryUniqueError("weekly_plan.user_week_revision", key)

            incoming_session_ids: set[UUID] = set()
            for session in snapshot.sessions:
                if session.id in incoming_session_ids:
                    raise RepositoryUniqueError("workout_session.id", session.id)
                incoming_session_ids.add(session.id)
                for indexed in self._store._sessions.values():
                    if (
                        indexed.session.id == session.id
                        and indexed.user_id != snapshot.user_id
                    ):
                        raise RepositoryUniqueError("workout_session.id", session.id)
                    if (
                        indexed.session.id == session.id
                        and indexed.plan_id != snapshot.id
                    ):
                        indexed_plan = self._store._plans[indexed.plan_id]
                        if indexed_plan.series_id != snapshot.series_id:
                            raise RepositoryUniqueError(
                                "workout_session.id", session.id
                            )

            if existing is not None:
                old_session_ids = {session.id for session in existing.sessions}
                for session_id in old_session_ids - incoming_session_ids:
                    self._store._sessions.pop((snapshot.id, session_id), None)

            if isinstance(snapshot.change_metadata, PlanChangeMetadata):
                request_key = (
                    snapshot.user_id,
                    snapshot.change_metadata.client_request_id,
                )
                request_owner = self._store._plan_id_by_replan_request.get(request_key)
                if request_owner not in {None, snapshot.id}:
                    raise RepositoryUniqueError(
                        "weekly_plan.user_replan_request", request_key
                    )
            self._store._plans[snapshot.id] = snapshot
            self._store._plan_id_by_revision[key] = snapshot.id
            if isinstance(snapshot.change_metadata, PlanChangeMetadata):
                self._store._plan_id_by_replan_request[request_key] = snapshot.id
            for session in snapshot.sessions:
                self._store._sessions[(snapshot.id, session.id)] = StoredSession(
                    user_id=snapshot.user_id,
                    plan_id=snapshot.id,
                    session=deepcopy(session),
                )
            return deepcopy(snapshot)

    async def list_by_user(self, user_id: UUID) -> list[WeeklyPlan]:
        async with self._store.lock:
            matches = [
                plan for plan in self._store._plans.values() if plan.user_id == user_id
            ]
            matches.sort(key=lambda item: (item.week_start, item.revision, item.id))
            return deepcopy(matches)

    async def list_sessions(self, plan_id: UUID) -> list[WorkoutSession]:
        async with self._store.lock:
            plan = self._store._plans.get(plan_id)
            if plan is None:
                return []
            sessions = list(plan.sessions)
            sessions.sort(key=lambda item: (item.scheduled_start, item.id))
            return deepcopy(sessions)

    async def list_sessions_for_user(
        self, plan_id: UUID, user_id: UUID
    ) -> list[WorkoutSession]:
        """List sessions only when the plan belongs to the requested user."""

        async with self._store.lock:
            plan = self._store._plans.get(plan_id)
            if plan is None or plan.user_id != user_id:
                return []
            sessions = list(plan.sessions)
            sessions.sort(key=lambda item: (item.scheduled_start, item.id))
            return deepcopy(sessions)

    async def get_session(self, session_id: UUID) -> WorkoutSession | None:
        async with self._store.lock:
            stored = self._select_current_session(session_id)
            return None if stored is None else deepcopy(stored.session)

    async def get_session_for_user(
        self, session_id: UUID, user_id: UUID
    ) -> WorkoutSession | None:
        """Return a session only when its aggregate belongs to the user."""

        async with self._store.lock:
            stored = self._select_current_session(session_id, user_id=user_id)
            if stored is None or stored.user_id != user_id:
                return None
            return deepcopy(stored.session)

    async def get_current_plan_for_session(
        self, session_id: UUID, user_id: UUID
    ) -> WeeklyPlan | None:
        async with self._store.lock:
            stored = self._select_current_session(session_id, user_id=user_id)
            if stored is None:
                return None
            return deepcopy(self._store._plans[stored.plan_id])

    async def list_revisions_for_user(
        self, series_id: UUID, user_id: UUID
    ) -> list[WeeklyPlan]:
        async with self._store.lock:
            revisions = [
                plan
                for plan in self._store._plans.values()
                if plan.user_id == user_id and plan.series_id == series_id
            ]
            revisions.sort(key=lambda item: (item.revision, item.id))
            return deepcopy(revisions)

    async def get_revision_for_user(
        self, series_id: UUID, user_id: UUID, revision: int
    ) -> WeeklyPlan | None:
        async with self._store.lock:
            matches = [
                plan
                for plan in self._store._plans.values()
                if plan.user_id == user_id
                and plan.series_id == series_id
                and plan.revision == revision
            ]
            if not matches:
                return None
            return deepcopy(matches[0])

    async def get_current_confirmed(
        self, series_id: UUID, user_id: UUID
    ) -> WeeklyPlan | None:
        async with self._store.lock:
            confirmed = [
                plan
                for plan in self._store._plans.values()
                if plan.user_id == user_id
                and plan.series_id == series_id
                and plan.status is WeeklyPlanStatus.CONFIRMED
            ]
            if not confirmed:
                return None
            confirmed.sort(key=lambda item: (item.revision, item.id), reverse=True)
            return deepcopy(confirmed[0])

    async def find_by_replan_request(
        self, user_id: UUID, client_request_id: str
    ) -> WeeklyPlan | None:
        async with self._store.lock:
            plan_id = self._store._plan_id_by_replan_request.get(
                (user_id, client_request_id)
            )
            return (
                None if plan_id is None else deepcopy(self._store._plans.get(plan_id))
            )

    async def get_generation_request(
        self, user_id: UUID, client_request_id: str
    ) -> tuple[str, WeeklyPlan] | None:
        async with self._store.lock:
            value = self._store._plan_generation_requests.get(
                (user_id, client_request_id)
            )
            if value is None:
                return None
            input_fingerprint, plan_id = value
            plan = self._store._plans.get(plan_id)
            if plan is None:
                return None
            return input_fingerprint, deepcopy(plan)

    async def bind_generation_request(
        self,
        user_id: UUID,
        client_request_id: str,
        input_fingerprint: str,
        plan_id: UUID,
    ) -> None:
        key = (user_id, client_request_id)
        async with self._store.lock:
            existing = self._store._plan_generation_requests.get(key)
            incoming = (input_fingerprint, plan_id)
            if existing is not None and existing != incoming:
                raise RepositoryUniqueError("weekly_plan.generation_request", key)
            self._store._plan_generation_requests[key] = incoming

    def _select_current_session(
        self, session_id: UUID, *, user_id: UUID | None = None
    ) -> StoredSession | None:
        candidates = [
            stored
            for stored in self._store._sessions.values()
            if stored.session.id == session_id
            and (user_id is None or stored.user_id == user_id)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                self._store._plans[item.plan_id].status is WeeklyPlanStatus.CONFIRMED,
                self._store._plans[item.plan_id].revision,
                item.plan_id,
            ),
            reverse=True,
        )
        return candidates[0]

    async def clear(self) -> None:
        """Clear plan aggregates and their derived session index."""

        async with self._store.lock:
            self._store._plans.clear()
            self._store._plan_id_by_revision.clear()
            self._store._sessions.clear()
            self._store._plan_id_by_replan_request.clear()
            self._store._plan_generation_requests.clear()
