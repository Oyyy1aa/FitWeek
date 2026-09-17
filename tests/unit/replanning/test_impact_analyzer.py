"""Deterministic change-impact classification."""

from datetime import UTC, datetime

import pytest

from app.domain.checkins.models import CheckInStatus
from app.domain.common import DomainValidationError, LocationType
from app.domain.exercises.catalog_seed import CATALOG_SEED
from app.domain.planning.models import AvailabilitySlot
from app.domain.replanning.models import LocalReplanCommand, PlanChangeType
from app.replanning.impact_analyzer import ChangeImpactAnalyzer
from tests.factories import make_plan
from tests.phase1a2_helpers import make_check_in

pytestmark = pytest.mark.phase_1a2

CATALOG = {item.id: item for item in CATALOG_SEED}


def command(change_type: PlanChangeType, **values: object) -> LocalReplanCommand:
    payload = {
        "client_request_id": "impact-1",
        "expected_plan_version": 2,
        "change_type": change_type,
        "effective_from": datetime(2026, 7, 20, 0, tzinfo=UTC),
        "replacement_availability_slots": (),
        "available_equipment": None,
        "max_session_minutes": None,
        "excluded_features": None,
    }
    payload.update(values)
    return LocalReplanCommand(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "status",
    [
        CheckInStatus.COMPLETED,
        CheckInStatus.PARTIALLY_COMPLETED,
        CheckInStatus.SKIPPED,
    ],
)
def test_checked_sessions_are_immutable(status: CheckInStatus) -> None:
    plan = make_plan()
    check_in = make_check_in(
        plan_id=plan.id,
        plan_revision=1,
        session_id=plan.sessions[0].id,
        status=status,
        actual_minutes=(None if status is CheckInStatus.SKIPPED else 10),
    )
    change = command(
        PlanChangeType.SESSION_DURATION_CHANGED,
        max_session_minutes=15,
    )

    impact = ChangeImpactAnalyzer().analyze(
        plan=plan,
        check_ins=(check_in,),
        change=change,
        exercise_catalog=CATALOG,
    )

    assert plan.sessions[0].id in impact.immutable_session_ids
    assert "IMMUTABLE_SESSION_CONFLICT" in {item.code for item in impact.reasons}


def test_session_before_effective_from_is_immutable_but_not_a_conflict() -> None:
    plan = make_plan()
    change = command(
        PlanChangeType.AVAILABILITY_CHANGED,
        effective_from=datetime(2026, 7, 21, tzinfo=UTC),
        replacement_availability_slots=(
            AvailabilitySlot(
                start=datetime(2026, 7, 22, 12, tzinfo=UTC),
                end=datetime(2026, 7, 22, 13, tzinfo=UTC),
                location_type=LocationType.HOME,
            ),
        ),
    )

    impact = ChangeImpactAnalyzer().analyze(
        plan=plan, check_ins=(), change=change, exercise_catalog=CATALOG
    )

    assert plan.sessions[0].id in impact.immutable_session_ids
    assert all(item.session_id != plan.sessions[0].id for item in impact.reasons)


def test_unaffected_future_session_is_preserved() -> None:
    plan = make_plan()
    change = command(
        PlanChangeType.SESSION_DURATION_CHANGED,
        max_session_minutes=45,
    )
    impact = ChangeImpactAnalyzer().analyze(
        plan=plan, check_ins=(), change=change, exercise_catalog=CATALOG
    )

    assert impact.preserved_session_ids == tuple(item.id for item in plan.sessions)
    assert impact.affected_session_ids == ()


@pytest.mark.parametrize(
    ("plan", "change", "reason"),
    [
        (
            make_plan(),
            command(
                PlanChangeType.AVAILABILITY_CHANGED,
                replacement_availability_slots=(
                    AvailabilitySlot(
                        start=datetime(2026, 7, 24, 12, tzinfo=UTC),
                        end=datetime(2026, 7, 24, 13, tzinfo=UTC),
                        location_type=LocationType.HOME,
                    ),
                ),
            ),
            "AVAILABILITY_CONFLICT",
        ),
        (
            make_plan(exercise_ids=("dumbbell_row",)),
            command(
                PlanChangeType.EQUIPMENT_CHANGED,
                available_equipment=(),
            ),
            "EQUIPMENT_CONFLICT",
        ),
        (
            make_plan(estimated_minutes=45),
            command(
                PlanChangeType.SESSION_DURATION_CHANGED,
                max_session_minutes=30,
            ),
            "SESSION_DURATION_CONFLICT",
        ),
        (
            make_plan(exercise_ids=("jumping_jack",)),
            command(
                PlanChangeType.EXCLUDED_FEATURE_CHANGED,
                excluded_features=("jumping",),
            ),
            "EXCLUDED_FEATURE_CONFLICT",
        ),
    ],
)
def test_change_types_affect_only_matching_sessions(plan, change, reason: str) -> None:
    impact = ChangeImpactAnalyzer().analyze(
        plan=plan, check_ins=(), change=change, exercise_catalog=CATALOG
    )

    assert impact.affected_session_ids
    assert reason in {item.code for item in impact.reasons}


def test_unsupported_change_type_is_rejected_at_domain_boundary() -> None:
    with pytest.raises(DomainValidationError) as captured:
        command("TRAINING_GOAL_CHANGED")  # type: ignore[arg-type]

    assert captured.value.code == "UNSUPPORTED_LOCAL_CHANGE"
