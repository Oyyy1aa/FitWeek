"""Deterministic preview, apply, atomicity, and reject tests."""

from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

import pytest

from app.application.errors import (
    BusinessRuleViolation,
    ProfileDraftApplyIdempotencyConflict,
    ProfileDraftExpired,
    ProfileDraftNotSupported,
    ProfileDraftRejected,
    ProfileDraftRejectIdempotencyConflict,
    ProfileDraftVersionConflict,
    ProfileVersionConflict,
)
from app.domain.profile_agent.apply_models import ProfileDraftApplyResult
from app.domain.profile_agent.errors import ProfileStateConflictError
from app.domain.profile_agent.models import (
    ConstraintProposal,
    ProfileDraftStatus,
    ScopeStatus,
)
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
    FitnessProfile,
    UserConstraint,
)
from tests.phase3b_helpers import (
    DRAFT_ID,
    NOW,
    USER_ID,
    build_harness,
    make_decision,
    make_draft,
    rich_output,
    save_draft,
)

pytestmark = pytest.mark.phase_3b


def existing_profile(*, version: int = 1) -> FitnessProfile:
    return FitnessProfile(
        id=UUID("31000000-0000-4000-8000-000000000001"),
        user_id=USER_ID,
        experience_level=ExperienceLevel.INTERMEDIATE,
        weekly_frequency=2,
        max_session_minutes=45,
        primary_goal=FitnessGoal.BASIC_STRENGTH,
        scope_confirmed=True,
        created_at=NOW - timedelta(days=20),
        updated_at=NOW - timedelta(days=1),
        version=version,
    )


@pytest.mark.asyncio
async def test_preview_is_deterministic_read_only_and_discloses_ignored_items() -> None:
    harness = build_harness()
    draft = await save_draft(harness)
    decision = make_decision()

    first = await harness.previews.preview(
        user=harness.user, draft_id=draft.id, decision=decision
    )
    second = await harness.previews.preview(
        user=harness.user, draft_id=draft.id, decision=decision
    )

    assert first == second
    assert first.validation.passed
    assert len(first.profile_changes) == 5
    assert len(first.constraints_to_add) == 4
    assert len(first.ignored_soft_preferences) == 1
    assert len(first.ignored_memory_candidates) == 1
    assert await harness.profiles.get_by_user_id(USER_ID) is None
    assert await harness.reviews.get_apply_result(draft.id, USER_ID) is None
    stored = await harness.reviews.get_draft_for_review(draft.id, USER_ID)
    assert stored is not None and stored.status is ProfileDraftStatus.PENDING_REVIEW


@pytest.mark.asyncio
async def test_apply_creates_complete_profile_selected_constraints_and_audit() -> None:
    harness = build_harness()
    draft = await save_draft(harness)

    committed = await harness.applications.apply(
        user=harness.user,
        draft_id=draft.id,
        decision=make_decision(),
    )

    profile = await harness.profiles.get_by_user_id(USER_ID)
    assert committed.created and profile is not None
    assert profile.weekly_frequency == 3
    assert profile.max_session_minutes == 30
    assert profile.primary_goal is FitnessGoal.GENERAL_FITNESS
    assert profile.experience_level is ExperienceLevel.BEGINNER
    assert profile.scope_confirmed and profile.version == 1
    constraints = await harness.profiles.list_constraints(profile.id)
    assert {item.constraint_type for item in constraints} == {
        ConstraintType.AVAILABLE_EQUIPMENT,
        ConstraintType.ALLOWED_LOCATION,
        ConstraintType.EXCLUDED_FEATURE,
        ConstraintType.UNAVAILABLE_TIME,
    }
    assert all(
        item.source is ConstraintSource.USER_CONFIRMED_AGENT_DRAFT
        for item in constraints
    )
    temporary = next(
        item
        for item in constraints
        if item.constraint_type is ConstraintType.UNAVAILABLE_TIME
    )
    assert temporary.valid_until == NOW + timedelta(days=10)
    assert committed.result.ignored_soft_preference_count == 1
    assert committed.result.ignored_memory_candidate_count == 1
    stored = await harness.reviews.get_draft_for_review(draft.id, USER_ID)
    assert stored is not None and stored.status is ProfileDraftStatus.APPLIED
    assert stored.applied_profile_id == profile.id


@pytest.mark.asyncio
async def test_existing_profile_selective_update_increments_version() -> None:
    harness = build_harness()
    await harness.profiles.save(existing_profile())
    draft = await save_draft(
        harness,
        make_draft(
            output=rich_output().model_copy(
                update={"goals": (FitnessGoal.GENERAL_FITNESS,)}
            )
        ),
    )
    base = make_decision(
        expected_profile_version=1,
        accepted_equipment=(),
        accepted_locations=(),
        accepted_hard_constraint_indexes=(),
        accepted_temporary_constraint_indexes=(),
        temporary_constraint_expirations={},
        confirmed_experience_level=None,
        confirm_scope=False,
    )
    decision = replace(
        base,
        accept_weekly_frequency=True,
        accept_max_session_minutes=False,
        selected_primary_goal=None,
    )

    await harness.applications.apply(
        user=harness.user, draft_id=draft.id, decision=decision
    )

    profile = await harness.profiles.get_by_user_id(USER_ID)
    assert profile is not None
    assert profile.weekly_frequency == 3
    assert profile.max_session_minutes == 45
    assert profile.primary_goal is FitnessGoal.BASIC_STRENGTH
    assert profile.experience_level is ExperienceLevel.INTERMEDIATE
    assert profile.version == 2


@pytest.mark.asyncio
async def test_duplicate_constraint_is_unchanged_and_not_written_twice() -> None:
    harness = build_harness()
    profile = await harness.profiles.save(existing_profile())
    existing = UserConstraint(
        id=uuid4(),
        profile_id=profile.id,
        constraint_type=ConstraintType.AVAILABLE_EQUIPMENT,
        constraint_value="resistance_band",
        priority=80,
        is_hard=True,
        source=ConstraintSource.USER_EXPLICIT,
        valid_until=None,
        created_at=NOW - timedelta(days=1),
        version=1,
    )
    await harness.profiles.add_constraint(existing)
    draft = await save_draft(harness)
    decision = make_decision(
        expected_profile_version=1,
        accepted_locations=(),
        accepted_hard_constraint_indexes=(),
        accepted_temporary_constraint_indexes=(),
        temporary_constraint_expirations={},
        confirmed_experience_level=None,
    )

    preview = await harness.previews.preview(
        user=harness.user, draft_id=draft.id, decision=decision
    )
    committed = await harness.applications.apply(
        user=harness.user, draft_id=draft.id, decision=decision
    )

    assert not preview.constraints_to_add
    assert [item.constraint_id for item in preview.constraints_unchanged] == [
        existing.id
    ]
    assert committed.result.unchanged_constraint_ids == (existing.id,)
    assert len(await harness.profiles.list_constraints(profile.id)) == 1


@pytest.mark.asyncio
async def test_single_value_constraint_conflict_blocks_apply_without_writes() -> None:
    harness = build_harness()
    profile = await harness.profiles.save(existing_profile())
    await harness.profiles.add_constraint(
        UserConstraint(
            id=uuid4(),
            profile_id=profile.id,
            constraint_type=ConstraintType.MAX_SESSION_MINUTES,
            constraint_value="30",
            priority=100,
            is_hard=True,
            source=ConstraintSource.USER_EXPLICIT,
            valid_until=None,
            created_at=NOW,
            version=1,
        )
    )
    output = rich_output().model_copy(
        update={
            "hard_constraints": (
                ConstraintProposal(
                    constraint_type=ConstraintType.MAX_SESSION_MINUTES,
                    value="45",
                    is_hard=True,
                    priority=100,
                ),
            ),
        }
    )
    draft = await save_draft(harness, make_draft(output=output))
    decision = make_decision(
        expected_profile_version=1,
        accepted_equipment=(),
        accepted_locations=(),
        accepted_temporary_constraint_indexes=(),
        temporary_constraint_expirations={},
        confirmed_experience_level=None,
    )

    preview = await harness.previews.preview(
        user=harness.user, draft_id=draft.id, decision=decision
    )
    assert not preview.validation.passed
    assert preview.constraints_conflicting
    with pytest.raises(BusinessRuleViolation, match="another value") as failure:
        await harness.applications.apply(
            user=harness.user, draft_id=draft.id, decision=decision
        )
    assert failure.value.code == "CONSTRAINT_CONFLICT"
    unchanged = await harness.profiles.get_by_user_id(USER_ID)
    assert unchanged is not None and unchanged.version == 1


@pytest.mark.asyncio
async def test_selection_version_scope_and_expiration_guards() -> None:
    harness = build_harness()
    draft = await save_draft(harness)
    with pytest.raises(BusinessRuleViolation) as missing_goal:
        await harness.previews.preview(
            user=harness.user,
            draft_id=draft.id,
            decision=replace(make_decision(), selected_primary_goal=None),
        )
    assert missing_goal.value.code == "PRIMARY_GOAL_SELECTION_REQUIRED"
    with pytest.raises(BusinessRuleViolation) as missing_expiration:
        await harness.previews.preview(
            user=harness.user,
            draft_id=draft.id,
            decision=make_decision(temporary_constraint_expirations={}),
        )
    assert missing_expiration.value.code == ("TEMPORARY_CONSTRAINT_EXPIRATION_REQUIRED")
    with pytest.raises(ProfileDraftVersionConflict):
        await harness.previews.preview(
            user=harness.user,
            draft_id=draft.id,
            decision=make_decision(expected_draft_version=2),
        )
    with pytest.raises(ProfileVersionConflict):
        await harness.previews.preview(
            user=harness.user,
            draft_id=draft.id,
            decision=make_decision(expected_profile_version=1),
        )

    unsupported = build_harness()
    unsupported_draft = await save_draft(
        unsupported,
        make_draft(output=rich_output(scope=ScopeStatus.NEEDS_REVIEW)),
    )
    with pytest.raises(ProfileDraftNotSupported):
        await unsupported.previews.preview(
            user=unsupported.user,
            draft_id=unsupported_draft.id,
            decision=make_decision(),
        )


@pytest.mark.asyncio
async def test_apply_idempotency_reuses_result_and_rejects_changed_payload() -> None:
    harness = build_harness()
    draft = await save_draft(harness)
    decision = make_decision()

    first = await harness.applications.apply(
        user=harness.user, draft_id=draft.id, decision=decision
    )
    second = await harness.applications.apply(
        user=harness.user, draft_id=draft.id, decision=decision
    )

    assert first.created and not second.created and first.result == second.result
    profile = await harness.profiles.get_by_user_id(USER_ID)
    assert profile is not None and profile.version == 1
    with pytest.raises(ProfileDraftApplyIdempotencyConflict):
        await harness.applications.apply(
            user=harness.user,
            draft_id=draft.id,
            decision=replace(decision, accepted_locations=()),
        )


@pytest.mark.asyncio
async def test_reject_is_idempotent_conflict_safe_and_blocks_apply() -> None:
    harness = build_harness()
    draft = await save_draft(harness)

    first = await harness.applications.reject(
        user=harness.user,
        draft_id=draft.id,
        client_request_id="reject-1",
        expected_draft_version=1,
    )
    second = await harness.applications.reject(
        user=harness.user,
        draft_id=draft.id,
        client_request_id="reject-1",
        expected_draft_version=1,
    )

    assert first.created and not second.created
    assert await harness.profiles.get_by_user_id(USER_ID) is None
    with pytest.raises(ProfileDraftRejectIdempotencyConflict):
        await harness.applications.reject(
            user=harness.user,
            draft_id=draft.id,
            client_request_id="reject-1",
            expected_draft_version=2,
        )
    with pytest.raises(ProfileDraftRejected):
        await harness.applications.apply(
            user=harness.user,
            draft_id=draft.id,
            decision=make_decision(),
        )


@pytest.mark.asyncio
async def test_expired_draft_is_not_applicable() -> None:
    harness = build_harness()
    expired = replace(
        make_draft(),
        created_at=NOW - timedelta(days=2),
        expires_at=NOW - timedelta(seconds=1),
    )
    await harness.drafts.save(expired)

    with pytest.raises(ProfileDraftExpired):
        await harness.applications.apply(
            user=harness.user,
            draft_id=expired.id,
            decision=make_decision(),
        )


@pytest.mark.asyncio
async def test_atomic_commit_revalidates_constraint_snapshot_before_any_write() -> None:
    harness = build_harness()
    draft = await save_draft(harness)
    decision = make_decision()
    merge = await harness.previews.build_merge(
        user=harness.user, draft_id=draft.id, decision=decision
    )
    injected = UserConstraint(
        id=uuid4(),
        profile_id=merge.profile.id,
        constraint_type=ConstraintType.EXCLUDED_FEATURE,
        constraint_value="running",
        priority=100,
        is_hard=True,
        source=ConstraintSource.USER_EXPLICIT,
        valid_until=None,
        created_at=NOW,
        version=1,
    )
    await harness.profiles.add_constraint(injected)
    result = ProfileDraftApplyResult(
        id=uuid5(UUID("32000000-0000-4000-8000-000000000001"), "atomic"),
        user_id=USER_ID,
        draft_id=draft.id,
        client_request_id=decision.client_request_id,
        apply_fingerprint=merge.preview.apply_fingerprint,
        profile_id=merge.profile.id,
        previous_profile_version=None,
        resulting_profile_version=1,
        added_constraint_ids=tuple(item.id for item in merge.constraints_to_add),
        unchanged_constraint_ids=(),
        ignored_soft_preference_count=1,
        ignored_memory_candidate_count=1,
        applied_at=NOW,
    )

    with pytest.raises(ProfileStateConflictError, match="constraints changed"):
        await harness.reviews.commit_apply(
            draft_id=draft.id,
            user_id=USER_ID,
            expected_draft_version=1,
            expected_profile_version=None,
            merge=merge,
            result=result,
            now=NOW,
        )

    assert await harness.profiles.get_by_user_id(USER_ID) is None
    assert await harness.reviews.get_apply_result(draft.id, USER_ID) is None
    stored = await harness.reviews.get_draft_for_review(DRAFT_ID, USER_ID)
    assert stored is not None and stored.status is ProfileDraftStatus.PENDING_REVIEW
