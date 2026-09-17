"""Deterministic Phase 3B fixtures with one shared in-memory state boundary."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.domain.common import LocationType
from app.domain.profile_agent.apply_models import ProfileDraftApplyDecision
from app.domain.profile_agent.models import (
    ConstraintProposal,
    MemoryCandidateProposal,
    PreferenceProposal,
    ProfileAgentDraft,
    ProfileAgentOutput,
    ScopeStatus,
)
from app.domain.profiles.models import (
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
)
from app.domain.users.models import UserAccount, UserStatus
from app.persistence.memory import (
    InMemoryProfileAgentDraftRepository,
    InMemoryProfileDraftReviewRepository,
    InMemoryProfileRepository,
    InMemoryStore,
)
from app.profile_application.apply import ProfileDraftApplyService
from app.profile_application.merge_policy import ProfileDraftMergePolicy
from app.profile_application.preview import ProfileDraftPreviewService

NOW = datetime(2026, 7, 20, 9, tzinfo=UTC)
USER_ID = UUID("00000000-0000-4000-8000-000000000001")
DRAFT_ID = UUID("30000000-0000-4000-8000-000000000001")


@dataclass(slots=True)
class Phase3BHarness:
    store: InMemoryStore
    user: UserAccount
    drafts: InMemoryProfileAgentDraftRepository
    reviews: InMemoryProfileDraftReviewRepository
    profiles: InMemoryProfileRepository
    previews: ProfileDraftPreviewService
    applications: ProfileDraftApplyService


def rich_output(*, scope: ScopeStatus = ScopeStatus.SUPPORTED) -> ProfileAgentOutput:
    return ProfileAgentOutput(
        weekly_frequency=3,
        max_session_minutes=30,
        goals=(FitnessGoal.GENERAL_FITNESS, FitnessGoal.BUILD_HABIT),
        hard_constraints=(
            ConstraintProposal(
                constraint_type=ConstraintType.EXCLUDED_FEATURE,
                value="jumping",
                is_hard=True,
                priority=100,
            ),
        ),
        soft_preferences=(
            PreferenceProposal(preference_type="TRAINING_TIME", value="morning"),
        ),
        temporary_constraints=(
            ConstraintProposal(
                constraint_type=ConstraintType.UNAVAILABLE_TIME,
                value="2026-07-22T09:00:00Z/2026-07-22T11:00:00Z",
                is_hard=True,
                priority=90,
            ),
        ),
        equipment=("resistance_band", "dumbbell"),
        locations=(LocationType.HOME, LocationType.GYM),
        scope_status=scope,
        missing_fields=(),
        memory_candidates=(
            MemoryCandidateProposal(
                category="PREFERENCE",
                value="prefers morning training",
                rationale="Explicitly proposed by the model for user review.",
            ),
        ),
        explanation_summary="Review each structured proposal before applying it.",
    )


def make_draft(
    *,
    output: ProfileAgentOutput | None = None,
    draft_id: UUID = DRAFT_ID,
) -> ProfileAgentDraft:
    return ProfileAgentDraft(
        id=draft_id,
        request_id=UUID("30000000-0000-4000-8000-000000000002"),
        client_request_id="parse-request-1",
        user_id=USER_ID,
        request_payload_fingerprint="a" * 64,
        input_fingerprint="b" * 64,
        output=output or rich_output(),
        prompt_version="profile-agent-v1",
        provider_summary="scripted-fake",
        fallback_used=False,
        fallback_type=None,
        created_at=NOW,
        expires_at=NOW + timedelta(days=2),
    )


def make_decision(
    *,
    client_request_id: str = "apply-request-1",
    expected_draft_version: int = 1,
    expected_profile_version: int | None = None,
    accepted_equipment: tuple[str, ...] = ("resistance_band",),
    accepted_locations: tuple[LocationType, ...] = (LocationType.HOME,),
    accepted_hard_constraint_indexes: tuple[int, ...] = (0,),
    accepted_temporary_constraint_indexes: tuple[int, ...] = (0,),
    temporary_constraint_expirations: dict[int, datetime] | None = None,
    confirmed_experience_level: ExperienceLevel | None = ExperienceLevel.BEGINNER,
    confirm_scope: bool = True,
) -> ProfileDraftApplyDecision:
    return ProfileDraftApplyDecision(
        client_request_id=client_request_id,
        expected_draft_version=expected_draft_version,
        expected_profile_version=expected_profile_version,
        accept_weekly_frequency=True,
        accept_max_session_minutes=True,
        selected_primary_goal=FitnessGoal.GENERAL_FITNESS,
        accepted_equipment=accepted_equipment,
        accepted_locations=accepted_locations,
        accepted_hard_constraint_indexes=accepted_hard_constraint_indexes,
        accepted_temporary_constraint_indexes=accepted_temporary_constraint_indexes,
        temporary_constraint_expirations=(
            temporary_constraint_expirations
            if temporary_constraint_expirations is not None
            else {0: NOW + timedelta(days=10)}
        ),
        confirmed_experience_level=confirmed_experience_level,
        confirm_scope=confirm_scope,
    )


def build_harness() -> Phase3BHarness:
    store = InMemoryStore()
    profiles = InMemoryProfileRepository(store)
    reviews = InMemoryProfileDraftReviewRepository(store, clock=lambda: NOW)
    previews = ProfileDraftPreviewService(
        reviews=reviews,
        profiles=profiles,
        merge_policy=ProfileDraftMergePolicy(),
        clock=lambda: NOW,
    )
    applications = ProfileDraftApplyService(
        reviews=reviews,
        previews=previews,
        clock=lambda: NOW,
    )
    user = UserAccount(
        id=USER_ID,
        email="dev-user@fitweek.local",
        timezone="UTC",
        status=UserStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        version=1,
    )
    return Phase3BHarness(
        store=store,
        user=user,
        drafts=InMemoryProfileAgentDraftRepository(store, clock=lambda: NOW),
        reviews=reviews,
        profiles=profiles,
        previews=previews,
        applications=applications,
    )


async def save_draft(
    harness: Phase3BHarness,
    draft: ProfileAgentDraft | None = None,
) -> ProfileAgentDraft:
    return await harness.drafts.save(draft or make_draft())


def with_request(
    decision: ProfileDraftApplyDecision,
    request_id: str,
) -> ProfileDraftApplyDecision:
    return replace(decision, client_request_id=request_id)


__all__ = [
    "DRAFT_ID",
    "NOW",
    "USER_ID",
    "Phase3BHarness",
    "build_harness",
    "make_decision",
    "make_draft",
    "rich_output",
    "save_draft",
    "with_request",
]
