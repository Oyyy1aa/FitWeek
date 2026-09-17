"""Idempotent Profile Draft apply, reject, and audit-query use cases."""

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid5

from app.application.errors import (
    BusinessRuleViolation,
    ProfileDraftAlreadyApplied,
    ProfileDraftApplyFailed,
    ProfileDraftApplyIdempotencyConflict,
    ProfileDraftExpired,
    ProfileDraftNotFound,
    ProfileDraftRejected,
    ProfileDraftRejectIdempotencyConflict,
    ProfileDraftVersionConflict,
    ProfileVersionConflict,
)
from app.domain.common import utc_now
from app.domain.profile_agent.apply_models import (
    APPLY_POLICY_VERSION,
    ProfileDraftApplyDecision,
    ProfileDraftApplyResult,
    ProfileDraftCommitResult,
    ProfileDraftRejectResult,
)
from app.domain.profile_agent.errors import (
    ProfileDraftAlreadyAppliedError,
    ProfileDraftApplyIdempotencyConflictError,
    ProfileDraftExpiredError,
    ProfileDraftNotFoundError,
    ProfileDraftRejectedError,
    ProfileDraftRejectIdempotencyConflictError,
    ProfileDraftVersionConflictError,
    ProfileStateConflictError,
    ProfileVersionConflictError,
)
from app.domain.profile_agent.repositories import ProfileDraftReviewRepository
from app.domain.users.models import UserAccount
from app.profile_application.merge_policy import build_apply_fingerprint
from app.profile_application.preview import ProfileDraftPreviewService

_APPLY_RESULT_NAMESPACE = UUID("18bfbc1b-5c56-4d57-a1b7-fcdd747cdcf1")


class ProfileDraftApplyService:
    def __init__(
        self,
        *,
        reviews: ProfileDraftReviewRepository,
        previews: ProfileDraftPreviewService,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._reviews = reviews
        self._previews = previews
        self._clock = clock

    async def apply(
        self,
        *,
        user: UserAccount,
        draft_id: UUID,
        decision: ProfileDraftApplyDecision,
    ) -> ProfileDraftCommitResult:
        fingerprint = build_apply_fingerprint(
            user_id=user.id,
            draft_id=draft_id,
            decision=decision,
        )
        existing = await self._reviews.get_apply_result(draft_id, user.id)
        if existing is not None:
            if (
                existing.client_request_id == decision.client_request_id
                and existing.apply_fingerprint == fingerprint
            ):
                return ProfileDraftCommitResult(result=existing, created=False)
            if existing.client_request_id == decision.client_request_id:
                raise ProfileDraftApplyIdempotencyConflict(
                    "Apply request id belongs to another payload."
                )
            raise ProfileDraftAlreadyApplied("Profile Draft is already applied.")
        merge = await self._previews.build_merge(
            user=user,
            draft_id=draft_id,
            decision=decision,
        )
        if not merge.preview.validation.passed:
            issue = merge.preview.validation.issues[0]
            raise BusinessRuleViolation(issue.message, code=issue.code.value)
        now = self._clock()
        result = ProfileDraftApplyResult(
            id=uuid5(
                _APPLY_RESULT_NAMESPACE,
                f"{user.id}:{decision.client_request_id}",
            ),
            user_id=user.id,
            draft_id=draft_id,
            client_request_id=decision.client_request_id,
            apply_fingerprint=fingerprint,
            profile_id=merge.profile.id,
            previous_profile_version=decision.expected_profile_version,
            resulting_profile_version=merge.profile.version,
            added_constraint_ids=tuple(item.id for item in merge.constraints_to_add),
            unchanged_constraint_ids=tuple(
                item.constraint_id
                for item in merge.preview.constraints_unchanged
                if item.constraint_id is not None
            ),
            ignored_soft_preference_count=len(merge.preview.ignored_soft_preferences),
            ignored_memory_candidate_count=len(merge.preview.ignored_memory_candidates),
            applied_at=now,
        )
        try:
            return await self._reviews.commit_apply(
                draft_id=draft_id,
                user_id=user.id,
                expected_draft_version=decision.expected_draft_version,
                expected_profile_version=decision.expected_profile_version,
                merge=merge,
                result=result,
                now=now,
            )
        except Exception as exc:
            self._raise_repository_failure(exc)
            raise ProfileDraftApplyFailed("Profile Draft apply failed.") from exc

    async def reject(
        self,
        *,
        user: UserAccount,
        draft_id: UUID,
        client_request_id: str,
        expected_draft_version: int,
    ) -> ProfileDraftRejectResult:
        request_id = client_request_id.strip()
        canonical = json.dumps(
            {
                "user_id": str(user.id),
                "draft_id": str(draft_id),
                "client_request_id": request_id,
                "expected_draft_version": expected_draft_version,
                "policy_version": APPLY_POLICY_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        now = self._clock()
        try:
            return await self._reviews.reject(
                draft_id=draft_id,
                user_id=user.id,
                client_request_id=request_id,
                expected_draft_version=expected_draft_version,
                reject_fingerprint=fingerprint,
                now=now,
            )
        except Exception as exc:
            self._raise_repository_failure(exc)
            raise ProfileDraftApplyFailed("Profile Draft reject failed.") from exc

    async def get_apply_result(
        self,
        *,
        user: UserAccount,
        draft_id: UUID,
    ) -> ProfileDraftApplyResult:
        result = await self._reviews.get_apply_result(draft_id, user.id)
        if result is None:
            raise ProfileDraftNotFound("Profile Draft apply result was not found.")
        return result

    @staticmethod
    def _raise_repository_failure(exc: Exception) -> None:
        mapping: tuple[tuple[type[Exception], type[Exception]], ...] = (
            (ProfileDraftNotFoundError, ProfileDraftNotFound),
            (ProfileDraftExpiredError, ProfileDraftExpired),
            (ProfileDraftAlreadyAppliedError, ProfileDraftAlreadyApplied),
            (ProfileDraftRejectedError, ProfileDraftRejected),
            (ProfileDraftVersionConflictError, ProfileDraftVersionConflict),
            (ProfileVersionConflictError, ProfileVersionConflict),
            (ProfileStateConflictError, ProfileVersionConflict),
            (
                ProfileDraftApplyIdempotencyConflictError,
                ProfileDraftApplyIdempotencyConflict,
            ),
            (
                ProfileDraftRejectIdempotencyConflictError,
                ProfileDraftRejectIdempotencyConflict,
            ),
        )
        for source, target in mapping:
            if isinstance(exc, source):
                raise target(str(exc)) from exc
