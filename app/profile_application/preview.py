"""Read-only Profile Draft preview service using the shared merge policy."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.application.errors import (
    BusinessRuleViolation,
    ProfileDraftAlreadyApplied,
    ProfileDraftExpired,
    ProfileDraftNotFound,
    ProfileDraftNotSupported,
    ProfileDraftRejected,
    ProfileDraftVersionConflict,
    ProfileVersionConflict,
)
from app.domain.common import utc_now
from app.domain.profile_agent.apply_models import (
    ProfileDraftApplyDecision,
    ProfileDraftApplyPreview,
    ProfileDraftMergeOutcome,
)
from app.domain.profile_agent.models import ProfileDraftStatus, ScopeStatus
from app.domain.profile_agent.repositories import ProfileDraftReviewRepository
from app.domain.profiles.models import UserConstraint
from app.domain.profiles.repositories import ProfileRepository
from app.domain.users.models import UserAccount
from app.profile_application.merge_policy import ProfileDraftMergePolicy
from app.profile_application.validation import ProfileDraftMergeError


class ProfileDraftPreviewService:
    def __init__(
        self,
        *,
        reviews: ProfileDraftReviewRepository,
        profiles: ProfileRepository,
        merge_policy: ProfileDraftMergePolicy,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._reviews = reviews
        self._profiles = profiles
        self._merge_policy = merge_policy
        self._clock = clock

    async def preview(
        self,
        *,
        user: UserAccount,
        draft_id: UUID,
        decision: ProfileDraftApplyDecision,
    ) -> ProfileDraftApplyPreview:
        return (
            await self.build_merge(user=user, draft_id=draft_id, decision=decision)
        ).preview

    async def build_merge(
        self,
        *,
        user: UserAccount,
        draft_id: UUID,
        decision: ProfileDraftApplyDecision,
    ) -> ProfileDraftMergeOutcome:
        now = self._clock()
        draft = await self._reviews.get_draft_for_review(draft_id, user.id)
        if draft is None:
            raise ProfileDraftNotFound("Profile Draft was not found.")
        if draft.expires_at <= now or draft.status is ProfileDraftStatus.EXPIRED:
            raise ProfileDraftExpired("Profile Draft has expired.")
        if draft.status is ProfileDraftStatus.APPLIED:
            raise ProfileDraftAlreadyApplied("Profile Draft is already applied.")
        if draft.status is ProfileDraftStatus.REJECTED:
            raise ProfileDraftRejected("Rejected Profile Draft cannot be applied.")
        if draft.version != decision.expected_draft_version:
            raise ProfileDraftVersionConflict("Profile Draft version changed.")
        if draft.output.scope_status is not ScopeStatus.SUPPORTED:
            raise ProfileDraftNotSupported(
                "Only a supported Profile Draft may be applied."
            )
        profile = await self._profiles.get_by_user_id(user.id)
        if profile is None:
            if decision.expected_profile_version is not None:
                raise ProfileVersionConflict(
                    "A new Profile requires expected_profile_version=null."
                )
            constraints: tuple[UserConstraint, ...] = ()
        else:
            if decision.expected_profile_version != profile.version:
                raise ProfileVersionConflict("Fitness Profile version changed.")
            constraints = tuple(await self._profiles.list_constraints(profile.id))
        try:
            return self._merge_policy.merge(
                user_id=user.id,
                draft=draft,
                current_profile=profile,
                existing_constraints=constraints,
                decision=decision,
                now=now,
            )
        except ProfileDraftMergeError as exc:
            raise BusinessRuleViolation(str(exc), code=exc.code.value) from exc
