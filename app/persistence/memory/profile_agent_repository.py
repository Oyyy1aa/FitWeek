"""TTL-bound Draft storage sharing the business store's atomic lock."""

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from app.domain.common import RepositoryUniqueError, utc_now
from app.domain.profile_agent.models import ProfileAgentDraft, ProfileDraftStatus
from app.persistence.memory.store import InMemoryStore


class InMemoryProfileAgentDraftRepository:
    """Store immutable draft copies for one process lifetime only."""

    def __init__(
        self,
        store: InMemoryStore | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store or InMemoryStore()
        self._clock = clock

    async def get(self, draft_id: UUID, user_id: UUID) -> ProfileAgentDraft | None:
        async with self._store.lock:
            draft = self._get_active_unlocked(draft_id)
            if draft is None or draft.user_id != user_id:
                return None
            return deepcopy(draft)

    async def get_by_client_request_id(
        self,
        user_id: UUID,
        client_request_id: str,
    ) -> ProfileAgentDraft | None:
        async with self._store.lock:
            key = (user_id, client_request_id)
            draft_id = self._store._profile_agent_draft_id_by_request.get(key)
            if draft_id is None:
                return None
            draft = self._get_active_unlocked(draft_id)
            if draft is None:
                self._store._profile_agent_draft_id_by_request.pop(key, None)
                return None
            return deepcopy(draft)

    async def save(self, draft: ProfileAgentDraft) -> ProfileAgentDraft:
        async with self._store.lock:
            key = (draft.user_id, draft.client_request_id)
            if draft.id in self._store._profile_agent_drafts:
                raise RepositoryUniqueError("profile_agent_draft.id", draft.id)
            if key in self._store._profile_agent_draft_id_by_request:
                raise RepositoryUniqueError(
                    "profile_agent_draft.user_request",
                    key,
                )
            stored = deepcopy(draft)
            self._store._profile_agent_drafts[stored.id] = stored
            self._store._profile_agent_draft_id_by_request[key] = stored.id
            return deepcopy(stored)

    async def list(
        self,
        user_id: UUID,
        *,
        status: ProfileDraftStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[ProfileAgentDraft]:
        async with self._store.lock:
            drafts = [
                deepcopy(draft)
                for draft in self._store._profile_agent_drafts.values()
                if draft.user_id == user_id
                and (status is None or draft.status is status)
            ]
            drafts.sort(key=lambda item: (item.created_at, item.id), reverse=True)
            return drafts[offset : offset + limit]

    async def reset(self) -> None:
        async with self._store.lock:
            self._store._profile_agent_drafts.clear()
            self._store._profile_agent_draft_id_by_request.clear()

    def _get_active_unlocked(self, draft_id: UUID) -> ProfileAgentDraft | None:
        draft = self._store._profile_agent_drafts.get(draft_id)
        if draft is None:
            return None
        now = self._clock()
        if (
            draft.status is ProfileDraftStatus.PENDING_REVIEW
            and draft.expires_at <= now
        ):
            self._store._profile_agent_drafts[draft.id] = draft.mark_expired()
            self._store._profile_agent_draft_id_by_request.pop(
                (draft.user_id, draft.client_request_id),
                None,
            )
            return None
        return draft
