"""Atomic in-memory Profile Draft apply/reject adapter."""

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from app.domain.common import utc_now
from app.domain.profile_agent.apply_models import (
    ProfileDraftApplyResult,
    ProfileDraftCommitResult,
    ProfileDraftMergeOutcome,
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
from app.domain.profile_agent.models import ProfileAgentDraft, ProfileDraftStatus
from app.domain.profiles.models import FitnessProfile
from app.persistence.memory.store import InMemoryStore


class InMemoryProfileDraftReviewRepository:
    """Commit already-validated immutable candidates under one short lock."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._clock = clock

    async def get_draft_for_review(
        self,
        draft_id: UUID,
        user_id: UUID,
    ) -> ProfileAgentDraft | None:
        async with self._store.lock:
            draft = self._store._profile_agent_drafts.get(draft_id)
            if draft is None or draft.user_id != user_id:
                return None
            if (
                draft.status is ProfileDraftStatus.PENDING_REVIEW
                and draft.expires_at <= self._clock()
            ):
                draft = draft.mark_expired()
                self._store._profile_agent_drafts[draft.id] = draft
                self._store._profile_agent_draft_id_by_request.pop(
                    (draft.user_id, draft.client_request_id),
                    None,
                )
            return deepcopy(draft)

    async def get_apply_result(
        self,
        draft_id: UUID,
        user_id: UUID,
    ) -> ProfileDraftApplyResult | None:
        async with self._store.lock:
            result_id = self._store._profile_draft_apply_id_by_draft.get(draft_id)
            if result_id is None:
                return None
            result = self._store._profile_draft_apply_results[result_id]
            if result.user_id != user_id:
                return None
            return deepcopy(result)

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
    ) -> ProfileDraftCommitResult:
        snapshot_profile = deepcopy(merge.profile)
        snapshot_constraints = deepcopy(merge.constraints_to_add)
        snapshot_result = deepcopy(result)
        async with self._store.lock:
            request_key = (user_id, result.client_request_id)
            existing_result_id = self._store._profile_draft_apply_id_by_request.get(
                request_key
            )
            if existing_result_id is not None:
                existing_result = self._store._profile_draft_apply_results[
                    existing_result_id
                ]
                if (
                    existing_result.draft_id != draft_id
                    or existing_result.apply_fingerprint != result.apply_fingerprint
                ):
                    raise ProfileDraftApplyIdempotencyConflictError(
                        "apply request id belongs to another payload"
                    )
                return ProfileDraftCommitResult(
                    result=deepcopy(existing_result),
                    created=False,
                )

            draft = self._require_draft(draft_id, user_id, now)
            if draft.status is ProfileDraftStatus.APPLIED:
                raise ProfileDraftAlreadyAppliedError("draft is already applied")
            if draft.status is ProfileDraftStatus.REJECTED:
                raise ProfileDraftRejectedError("rejected draft cannot be applied")
            if draft.status is ProfileDraftStatus.EXPIRED:
                raise ProfileDraftExpiredError("expired draft cannot be applied")
            if draft.version != expected_draft_version:
                raise ProfileDraftVersionConflictError("draft version changed")

            current_profile = self._current_profile(user_id)
            if current_profile is None:
                if (
                    expected_profile_version is not None
                    or snapshot_profile.version != 1
                ):
                    raise ProfileVersionConflictError("new profile version changed")
                if self._store._profile_id_by_user.get(user_id) is not None:
                    raise ProfileVersionConflictError(
                        "profile was created concurrently"
                    )
            else:
                if (
                    expected_profile_version != current_profile.version
                    or snapshot_profile.id != current_profile.id
                    or snapshot_profile.version != current_profile.version + 1
                ):
                    raise ProfileVersionConflictError("profile version changed")

            current_constraint_versions = tuple(
                sorted(
                    (item.id, item.version)
                    for item in self._store._constraints.values()
                    if item.profile_id == snapshot_profile.id
                )
            )
            if current_constraint_versions != merge.expected_constraint_versions:
                raise ProfileStateConflictError("profile constraints changed")
            if any(
                item.id in self._store._constraints
                or item.profile_id != snapshot_profile.id
                for item in snapshot_constraints
            ):
                raise ProfileStateConflictError("constraint candidates changed")
            if snapshot_result.profile_id != snapshot_profile.id:
                raise ProfileStateConflictError("apply result profile is inconsistent")

            marked = draft.mark_applied(
                applied_at=now,
                profile_id=snapshot_profile.id,
                apply_request_id=snapshot_result.client_request_id,
            )
            # Every object is validated and detached before the first assignment.
            self._store._profiles[snapshot_profile.id] = snapshot_profile
            self._store._profile_id_by_user[user_id] = snapshot_profile.id
            for constraint in snapshot_constraints:
                self._store._constraints[constraint.id] = constraint
            self._store._profile_draft_apply_results[snapshot_result.id] = (
                snapshot_result
            )
            self._store._profile_draft_apply_id_by_request[request_key] = (
                snapshot_result.id
            )
            self._store._profile_draft_apply_id_by_draft[draft_id] = snapshot_result.id
            self._store._profile_agent_drafts[draft_id] = marked
            return ProfileDraftCommitResult(
                result=deepcopy(snapshot_result), created=True
            )

    async def reject(
        self,
        *,
        draft_id: UUID,
        user_id: UUID,
        client_request_id: str,
        expected_draft_version: int,
        reject_fingerprint: str,
        now: datetime,
    ) -> ProfileDraftRejectResult:
        key = (user_id, client_request_id)
        async with self._store.lock:
            existing = self._store._profile_draft_reject_by_request.get(key)
            if existing is not None:
                existing_draft_id, existing_fingerprint, rejected_at = existing
                if (
                    existing_draft_id != draft_id
                    or existing_fingerprint != reject_fingerprint
                ):
                    raise ProfileDraftRejectIdempotencyConflictError(
                        "reject request id belongs to another payload"
                    )
                return ProfileDraftRejectResult(
                    draft_id=draft_id,
                    user_id=user_id,
                    client_request_id=client_request_id,
                    reject_fingerprint=reject_fingerprint,
                    rejected_at=rejected_at,
                    created=False,
                )
            draft = self._require_draft(draft_id, user_id, now)
            if draft.status is ProfileDraftStatus.APPLIED:
                raise ProfileDraftAlreadyAppliedError(
                    "applied draft cannot be rejected"
                )
            if draft.status is ProfileDraftStatus.REJECTED:
                raise ProfileDraftRejectedError("draft is already rejected")
            if draft.status is ProfileDraftStatus.EXPIRED:
                raise ProfileDraftExpiredError("expired draft cannot be rejected")
            if draft.version != expected_draft_version:
                raise ProfileDraftVersionConflictError("draft version changed")
            rejected = draft.mark_rejected(rejected_at=now)
            self._store._profile_agent_drafts[draft.id] = rejected
            self._store._profile_draft_reject_by_request[key] = (
                draft.id,
                reject_fingerprint,
                now,
            )
            return ProfileDraftRejectResult(
                draft_id=draft_id,
                user_id=user_id,
                client_request_id=client_request_id,
                reject_fingerprint=reject_fingerprint,
                rejected_at=now,
                created=True,
            )

    def _require_draft(
        self,
        draft_id: UUID,
        user_id: UUID,
        now: datetime,
    ) -> ProfileAgentDraft:
        draft = self._store._profile_agent_drafts.get(draft_id)
        if draft is None or draft.user_id != user_id:
            raise ProfileDraftNotFoundError("draft was not found")
        if (
            draft.status is ProfileDraftStatus.PENDING_REVIEW
            and draft.expires_at <= now
        ):
            draft = draft.mark_expired()
            self._store._profile_agent_drafts[draft.id] = draft
            self._store._profile_agent_draft_id_by_request.pop(
                (draft.user_id, draft.client_request_id),
                None,
            )
        return draft

    def _current_profile(self, user_id: UUID) -> FitnessProfile | None:
        profile_id = self._store._profile_id_by_user.get(user_id)
        return self._store._profiles.get(profile_id) if profile_id is not None else None
