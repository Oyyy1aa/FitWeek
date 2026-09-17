"""Concurrency-safe process-local Session Design adapter."""

from copy import deepcopy
from uuid import UUID

from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.session_design.models import (
    ExerciseCandidateSet,
    SessionDesignDraft,
    SessionDesignTrace,
)
from app.persistence.memory.store import InMemoryStore


class InMemorySessionDesignRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> SessionDesignDraft | None:
        async with self._store.lock:
            value = self._store._session_design_drafts.get(draft_id)
            return (
                deepcopy(value)
                if value is not None and value.user_id == user_id
                else None
            )

    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> SessionDesignDraft | None:
        async with self._store.lock:
            draft_id = self._store._session_design_id_by_request.get(
                (user_id, client_request_id)
            )
            return (
                deepcopy(self._store._session_design_drafts.get(draft_id))
                if draft_id
                else None
            )

    async def save_candidate_set(
        self, candidate_set: ExerciseCandidateSet
    ) -> ExerciseCandidateSet:
        async with self._store.lock:
            current = self._store._session_candidate_sets.get(candidate_set.id)
            if current is not None:
                if current.fingerprint != candidate_set.fingerprint:
                    raise RepositoryUniqueError(
                        "session_candidate_set.id", candidate_set.id
                    )
                return deepcopy(current)
            self._store._session_candidate_sets[candidate_set.id] = deepcopy(
                candidate_set
            )
            return deepcopy(candidate_set)

    async def get_candidate_set(
        self, user_id: UUID, candidate_set_id: UUID
    ) -> ExerciseCandidateSet | None:
        async with self._store.lock:
            value = self._store._session_candidate_sets.get(candidate_set_id)
            return (
                deepcopy(value)
                if value is not None and value.user_id == user_id
                else None
            )

    async def save_draft(
        self, draft: SessionDesignDraft, trace: SessionDesignTrace
    ) -> SessionDesignDraft:
        async with self._store.lock:
            key = (draft.user_id, draft.client_request_id)
            if (
                draft.id in self._store._session_design_drafts
                or key in self._store._session_design_id_by_request
            ):
                raise RepositoryUniqueError("session_design.request", key)
            self._store._session_design_drafts[draft.id] = deepcopy(draft)
            self._store._session_design_id_by_request[key] = draft.id
            self._store._session_design_traces[draft.id] = deepcopy(trace)
            return deepcopy(draft)

    async def update_draft(self, draft: SessionDesignDraft) -> SessionDesignDraft:
        async with self._store.lock:
            current = self._store._session_design_drafts.get(draft.id)
            if current is None:
                raise RepositoryUniqueError("session_design.id", draft.id)
            expected = current.version + 1
            if draft.version != expected:
                raise RepositoryConflictError(
                    "SessionDesignDraft",
                    draft.id,
                    expected_version=expected,
                    actual_version=draft.version,
                )
            self._store._session_design_drafts[draft.id] = deepcopy(draft)
            return deepcopy(draft)

    async def get_trace(
        self, user_id: UUID, draft_id: UUID
    ) -> SessionDesignTrace | None:
        async with self._store.lock:
            draft = self._store._session_design_drafts.get(draft_id)
            value = self._store._session_design_traces.get(draft_id)
            return (
                deepcopy(value)
                if draft is not None and draft.user_id == user_id and value is not None
                else None
            )

    async def clear(self) -> None:
        async with self._store.lock:
            self._store._session_candidate_sets.clear()
            self._store._session_design_drafts.clear()
            self._store._session_design_id_by_request.clear()
            self._store._session_design_traces.clear()
