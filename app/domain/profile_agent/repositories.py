"""Profile Draft storage and atomic review-application contracts."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.profile_agent.apply_models import (
    ProfileDraftApplyResult,
    ProfileDraftCommitResult,
    ProfileDraftMergeOutcome,
    ProfileDraftRejectResult,
)
from app.domain.profile_agent.models import ProfileAgentDraft, ProfileDraftStatus


class ProfileAgentDraftRepository(Protocol):
    async def get(self, draft_id: UUID, user_id: UUID) -> ProfileAgentDraft | None: ...

    async def get_by_client_request_id(
        self,
        user_id: UUID,
        client_request_id: str,
    ) -> ProfileAgentDraft | None: ...

    async def save(self, draft: ProfileAgentDraft) -> ProfileAgentDraft: ...

    async def list(
        self,
        user_id: UUID,
        *,
        status: ProfileDraftStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[ProfileAgentDraft]: ...

    async def reset(self) -> None: ...


class ProfileDraftReviewRepository(Protocol):
    """Port for review reads and one-lock memory Compare-and-Swap commits."""

    async def get_draft_for_review(
        self,
        draft_id: UUID,
        user_id: UUID,
    ) -> ProfileAgentDraft | None: ...

    async def get_apply_result(
        self,
        draft_id: UUID,
        user_id: UUID,
    ) -> ProfileDraftApplyResult | None: ...

    async def commit_apply(
        self,
        *,
        draft_id: UUID,
        user_id: UUID,
        expected_draft_version: int,
        expected_profile_version: int | None,
        merge: ProfileDraftMergeOutcome,
        result: ProfileDraftApplyResult,
        now: datetime,
    ) -> ProfileDraftCommitResult: ...

    async def reject(
        self,
        *,
        draft_id: UUID,
        user_id: UUID,
        client_request_id: str,
        expected_draft_version: int,
        reject_fingerprint: str,
        now: datetime,
    ) -> ProfileDraftRejectResult: ...
