"""Persistence ports for frozen candidate sets and Session Design Drafts."""

from typing import Protocol
from uuid import UUID

from app.domain.session_design.models import (
    ExerciseCandidateSet,
    SessionDesignDraft,
    SessionDesignTrace,
)


class SessionDesignRepository(Protocol):
    async def get_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> SessionDesignDraft | None: ...
    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> SessionDesignDraft | None: ...
    async def save_candidate_set(
        self, candidate_set: ExerciseCandidateSet
    ) -> ExerciseCandidateSet: ...
    async def get_candidate_set(
        self, user_id: UUID, candidate_set_id: UUID
    ) -> ExerciseCandidateSet | None: ...
    async def save_draft(
        self, draft: SessionDesignDraft, trace: SessionDesignTrace
    ) -> SessionDesignDraft: ...
    async def update_draft(self, draft: SessionDesignDraft) -> SessionDesignDraft: ...
    async def get_trace(
        self, user_id: UUID, draft_id: UUID
    ) -> SessionDesignTrace | None: ...
    async def clear(self) -> None: ...
