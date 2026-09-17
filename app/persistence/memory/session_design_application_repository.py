"""Atomic process-local commit for Draft, Plan Revision, and Apply Result."""

from copy import deepcopy
from uuid import UUID

from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.session_design.models import SessionDesignDraft
from app.domain.session_design_application.models import (
    SessionDesignApplicationCommit,
    SessionDesignApplicationResult,
)
from app.persistence.memory.store import InMemoryStore, StoredSession


class InMemorySessionDesignApplicationRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self._fail_next_commit = False

    def fail_next_commit_for_test(self) -> None:
        self._fail_next_commit = True

    async def get_result(
        self, user_id: UUID, result_id: UUID
    ) -> SessionDesignApplicationResult | None:
        async with self._store.lock:
            result = self._store._session_design_application_results.get(result_id)
            return deepcopy(result) if result and result.user_id == user_id else None

    async def get_result_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> SessionDesignApplicationResult | None:
        async with self._store.lock:
            result_id = self._store._session_design_application_id_by_request.get(
                (user_id, client_request_id)
            )
            return (
                deepcopy(self._store._session_design_application_results[result_id])
                if result_id is not None
                else None
            )

    async def get_result_by_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> SessionDesignApplicationResult | None:
        async with self._store.lock:
            result_id = self._store._session_design_application_id_by_draft.get(
                draft_id
            )
            if result_id is None:
                return None
            result = self._store._session_design_application_results[result_id]
            return deepcopy(result) if result.user_id == user_id else None

    async def commit(
        self,
        *,
        source: WeeklyPlan,
        expected_draft: SessionDesignDraft,
        applied_draft: SessionDesignDraft,
        revision: WeeklyPlan,
        result: SessionDesignApplicationResult,
    ) -> SessionDesignApplicationCommit:
        revision_snapshot = deepcopy(revision)
        draft_snapshot = deepcopy(applied_draft)
        result_snapshot = deepcopy(result)
        request_key = (result.user_id, result.client_request_id)
        revision_key = (
            revision.user_id,
            revision.week_start,
            revision.revision,
        )
        async with self._store.lock:
            existing_id = self._store._session_design_application_id_by_request.get(
                request_key
            )
            if existing_id is not None:
                existing = self._store._session_design_application_results[existing_id]
                if existing.application_fingerprint != result.application_fingerprint:
                    raise RepositoryUniqueError(
                        "session_design_application.user_request", request_key
                    )
                plan_id = self._store._plan_id_by_revision[
                    (existing.user_id, source.week_start, existing.created_revision)
                ]
                return SessionDesignApplicationCommit(
                    result=deepcopy(existing),
                    plan_revision_id=plan_id,
                    draft_id=existing.draft_id,
                    created=False,
                )
            current_draft = self._store._session_design_drafts.get(expected_draft.id)
            if current_draft is None or current_draft.user_id != result.user_id:
                raise RepositoryConflictError(
                    "SessionDesignDraft",
                    expected_draft.id,
                    expected_version=expected_draft.version,
                    actual_version=0,
                )
            if (
                current_draft.version != expected_draft.version
                or current_draft.status != expected_draft.status
            ):
                raise RepositoryConflictError(
                    "SessionDesignDraft",
                    expected_draft.id,
                    expected_version=expected_draft.version,
                    actual_version=current_draft.version,
                )
            stored_source = self._store._plans.get(source.id)
            if stored_source is None or stored_source.version != source.version:
                raise RepositoryConflictError(
                    "WeeklyPlan",
                    source.id,
                    expected_version=source.version,
                    actual_version=-1
                    if stored_source is None
                    else stored_source.version,
                )
            confirmed = sorted(
                (
                    item
                    for item in self._store._plans.values()
                    if item.user_id == source.user_id
                    and item.series_id == source.series_id
                    and item.status is WeeklyPlanStatus.CONFIRMED
                ),
                key=lambda item: (item.revision, str(item.id)),
                reverse=True,
            )
            if not confirmed or confirmed[0].id != source.id:
                raise RepositoryConflictError(
                    "WeeklyPlan.current_revision",
                    source.series_id,
                    expected_version=source.revision,
                    actual_version=confirmed[0].revision if confirmed else -1,
                )
            if revision_key in self._store._plan_id_by_revision:
                raise RepositoryUniqueError(
                    "weekly_plan.user_week_revision", revision_key
                )
            if expected_draft.id in self._store._session_design_application_id_by_draft:
                raise RepositoryUniqueError(
                    "session_design_application.draft", expected_draft.id
                )
            if self._fail_next_commit:
                self._fail_next_commit = False
                raise RuntimeError("Injected atomic commit failure.")

            self._store._plans[revision_snapshot.id] = revision_snapshot
            self._store._plan_id_by_revision[revision_key] = revision_snapshot.id
            for session in revision_snapshot.sessions:
                self._store._sessions[(revision_snapshot.id, session.id)] = (
                    StoredSession(
                        user_id=revision_snapshot.user_id,
                        plan_id=revision_snapshot.id,
                        session=deepcopy(session),
                    )
                )
            self._store._session_design_application_results[result_snapshot.id] = (
                result_snapshot
            )
            self._store._session_design_application_id_by_request[request_key] = (
                result_snapshot.id
            )
            self._store._session_design_application_id_by_draft[expected_draft.id] = (
                result_snapshot.id
            )
            self._store._session_design_drafts[expected_draft.id] = draft_snapshot
            return SessionDesignApplicationCommit(
                result=deepcopy(result_snapshot),
                plan_revision_id=revision_snapshot.id,
                draft_id=draft_snapshot.id,
                created=True,
            )

    async def clear(self) -> None:
        async with self._store.lock:
            self._store._session_design_application_results.clear()
            self._store._session_design_application_id_by_request.clear()
            self._store._session_design_application_id_by_draft.clear()
