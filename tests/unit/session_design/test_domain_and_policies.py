"""Phase 5A domain, template, candidate, duration, and whitelist tests."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.common import DomainValidationError, LocationType
from app.domain.context.enums import ContextDegradedMode
from app.domain.exercises.catalog_seed import CATALOG_SEED
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
    FitnessProfile,
    UserConstraint,
)
from app.domain.session_design.enums import (
    SessionDesignDraftStatus,
    SessionDesignSource,
    SessionTemplateId,
)
from app.domain.session_design.models import (
    ExerciseCandidateSet,
    SessionDesignDraft,
    SessionDesignerOutput,
    SessionDesignerSelection,
)
from app.domain.sessions.models import SessionExercise
from app.safety.models import SafetyValidationResult
from app.session_design.candidate_search import ExerciseCandidateSearcher
from app.session_design.duration import SessionDurationPolicy
from app.session_design.fallback import DeterministicSessionFallback
from app.session_design.template_registry import SessionTemplateRegistry
from app.session_design.validator import SessionDesignerBusinessValidator

pytestmark = pytest.mark.phase_5a


def profile(level: ExperienceLevel = ExperienceLevel.BEGINNER) -> FitnessProfile:
    now = datetime(2026, 7, 20, tzinfo=UTC)
    return FitnessProfile(
        id=uuid4(),
        user_id=uuid4(),
        experience_level=level,
        weekly_frequency=3,
        max_session_minutes=45,
        primary_goal=FitnessGoal.GENERAL_FITNESS,
        scope_confirmed=True,
        created_at=now,
        updated_at=now,
        version=1,
    )


def constraint(
    owner: FitnessProfile, kind: ConstraintType, value: str
) -> UserConstraint:
    return UserConstraint(
        id=uuid4(),
        profile_id=owner.id,
        constraint_type=kind,
        constraint_value=value,
        priority=100,
        is_hard=True,
        source=ConstraintSource.USER_EXPLICIT,
        valid_until=None,
        created_at=datetime(2026, 7, 19, tzinfo=UTC),
        version=1,
    )


def candidate_fixture() -> tuple[
    FitnessProfile, tuple[UserConstraint, ...], object, ExerciseCandidateSet
]:
    owner = profile()
    constraints = ()
    template = SessionTemplateRegistry().get(SessionTemplateId.FULL_BODY_BASIC)
    slots = ExerciseCandidateSearcher().search(
        profile=owner,
        constraints=constraints,
        catalog=CATALOG_SEED,
        template=template,
        location=LocationType.HOME,
        target_date=date(2026, 7, 20),
    )
    candidates = ExerciseCandidateSet(
        id=uuid4(),
        user_id=owner.user_id,
        request_fingerprint="request",
        fingerprint="candidate",
        template_id=template.id,
        template_version=template.version,
        catalog_version="catalog",
        context_snapshot_reference_id=uuid4(),
        context_fingerprint="context",
        slots=slots,
        created_at=datetime(2026, 7, 19, tzinfo=UTC),
    )
    return owner, constraints, template, candidates


def test_registry_contains_all_six_templates_and_required_roles() -> None:
    registry = SessionTemplateRegistry()
    for template_id in SessionTemplateId:
        template = registry.get(template_id)
        assert template.id is template_id
        assert {item.role.value for item in template.slots} == {
            "WARMUP",
            "MAIN",
            "COOLDOWN",
        }


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        (FitnessGoal.MOBILITY, SessionTemplateId.MOBILITY_RECOVERY),
        (FitnessGoal.LOW_IMPACT_CARDIO, SessionTemplateId.LOW_IMPACT_CARDIO),
        (FitnessGoal.BASIC_STRENGTH, SessionTemplateId.FULL_BODY_BASIC),
        (FitnessGoal.MIXED, SessionTemplateId.MIXED_HOME),
    ],
)
def test_template_selection_is_deterministic(
    goal: FitnessGoal, expected: SessionTemplateId
) -> None:
    selected = SessionTemplateRegistry().select(
        goal=goal,
        location=LocationType.HOME,
        preferred_type=None,
        requested=None,
        experience_level=ExperienceLevel.BEGINNER,
        target_duration_minutes=30,
    )
    assert selected.id is expected


def test_candidate_search_filters_equipment_feature_and_beginner_difficulty() -> None:
    owner = profile()
    constraints = (constraint(owner, ConstraintType.EXCLUDED_FEATURE, "jumping"),)
    template = SessionTemplateRegistry().get(SessionTemplateId.FULL_BODY_BASIC)
    slots = ExerciseCandidateSearcher().search(
        profile=owner,
        constraints=constraints,
        catalog=CATALOG_SEED,
        template=template,
        location=LocationType.HOME,
        target_date=date(2026, 7, 20),
    )
    selected_ids = {value for slot in slots for value in slot.exercise_ids}
    by_id = {item.id: item for item in CATALOG_SEED}
    assert "burpee" not in selected_ids
    assert "jumping_jack" not in selected_ids
    assert all(not by_id[item].required_equipment for item in selected_ids)
    assert all(
        by_id[item].difficulty_level.value == "BEGINNER" for item in selected_ids
    )


def test_candidate_slot_rejects_empty_search_result() -> None:
    owner = profile()
    template = SessionTemplateRegistry().get(SessionTemplateId.MOBILITY_RECOVERY)
    with pytest.raises(DomainValidationError):
        ExerciseCandidateSearcher().search(
            profile=owner,
            constraints=(),
            catalog=(),
            template=template,
            location=LocationType.HOME,
            target_date=date(2026, 7, 20),
        )


def test_fallback_uses_only_candidate_ids_once_and_in_slot_order() -> None:
    _, _, template, candidates = candidate_fixture()
    output = DeterministicSessionFallback().build(candidates, template)  # type: ignore[arg-type]
    assert tuple(item.slot_id for item in output.selections) == tuple(
        item.slot_id for item in candidates.slots
    )
    assert len({item.exercise_id for item in output.selections}) == len(
        output.selections
    )


def test_whitelist_rejects_exercise_outside_frozen_candidate_set() -> None:
    owner, constraints, template, candidates = candidate_fixture()
    output = DeterministicSessionFallback().build(candidates, template)  # type: ignore[arg-type]
    changed = output.model_copy(
        update={
            "selections": (
                output.selections[0].model_copy(
                    update={"exercise_id": "invented_move"}
                ),
                *output.selections[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="outside the frozen Candidate Set"):
        SessionDesignerBusinessValidator().validate(
            changed,
            candidate_set=candidates,
            template=template,  # type: ignore[arg-type]
            profile=owner,
            constraints=constraints,
            catalog={item.id: item for item in CATALOG_SEED},
        )


def test_output_schema_forbids_extra_fields() -> None:
    with pytest.raises(ValueError):
        SessionDesignerOutput.model_validate(
            {
                "template_id": "FULL_BODY_BASIC",
                "session_type": "STRENGTH",
                "selections": [],
                "explanation_summary": "x",
                "python": "print('unsafe')",
            }
        )


def test_duration_policy_is_exact_and_versioned() -> None:
    exercises = tuple(
        SessionExercise(
            exercise_id=f"move_{index}",
            sequence_no=index,
            sets=None,
            repetitions=None,
            duration_seconds=60,
            rest_seconds=15,
        )
        for index in range(1, 5)
    )
    fitted, breakdown = SessionDurationPolicy().fit_exact(exercises, 30)
    assert breakdown.total_seconds == 1800
    assert breakdown.policy_version == "session-duration-policy-v1"
    assert tuple(item.sequence_no for item in fitted) == (1, 2, 3, 4)


def test_draft_review_lifecycle_is_terminal() -> None:
    owner, _, template, candidates = candidate_fixture()
    output = DeterministicSessionFallback().build(candidates, template)  # type: ignore[arg-type]
    exercises = tuple(
        SessionExercise(
            exercise_id=item.exercise_id,
            sequence_no=index,
            sets=None,
            repetitions=None,
            duration_seconds=60,
            rest_seconds=0,
        )
        for index, item in enumerate(output.selections, start=1)
    )
    fitted, duration = SessionDurationPolicy().fit_exact(exercises, 15)
    now = datetime(2026, 7, 19, tzinfo=UTC)
    draft = SessionDesignDraft(
        id=uuid4(),
        request_id=uuid4(),
        client_request_id="request",
        user_id=owner.user_id,
        request_payload_fingerprint="payload",
        candidate_set_id=candidates.id,
        candidate_set_fingerprint=candidates.fingerprint,
        context_snapshot_reference_id=candidates.context_snapshot_reference_id,
        context_fingerprint=candidates.context_fingerprint,
        context_degraded_mode=ContextDegradedMode.NONE,
        template_id=candidates.template_id,
        template_version=candidates.template_version,
        catalog_version=candidates.catalog_version,
        session_type=template.session_type,  # type: ignore[union-attr]
        target_date=date(2026, 7, 20),
        target_duration_minutes=15,
        location=LocationType.HOME,
        goal=FitnessGoal.GENERAL_FITNESS,
        exercises=fitted,
        exercise_roles=tuple(slot.role for slot in candidates.slots),
        duration=duration,
        safety_validation=SafetyValidationResult.from_violations(()),
        source=SessionDesignSource.TEMPLATE_FALLBACK,
        prompt_version="session-designer-v1",
        provider_summary="template-fallback",
        fallback_used=True,
        explanation_summary="review",
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    accepted = draft.accept(now + timedelta(minutes=1))
    assert accepted.status is SessionDesignDraftStatus.ACCEPTED
    assert accepted.version == 2
    with pytest.raises(DomainValidationError):
        accepted.reject(now + timedelta(minutes=2))


def test_selection_schema_requires_positive_work_parameters() -> None:
    with pytest.raises(ValueError):
        SessionDesignerSelection(
            slot_id="main",
            exercise_id="bodyweight_squat",
            repetitions=0,
            rest_seconds=0,
        )
