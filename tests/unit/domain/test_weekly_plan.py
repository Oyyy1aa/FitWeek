"""Weekly-plan aggregate invariants and confirmation transition."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.domain.common import (
    DomainConflictError,
    DomainValidationError,
    InvalidDomainStateTransition,
)
from app.domain.plans.models import WeeklyPlanStatus
from tests.factories import TEST_NOW, TEST_WEEK_START, make_plan

pytestmark = pytest.mark.phase_1a


def test_week_start_must_be_monday() -> None:
    with pytest.raises(DomainValidationError, match="week_start must be a Monday"):
        make_plan(week_start=date(2026, 7, 21))


def test_plan_rejects_incorrect_session_total() -> None:
    plan = make_plan()

    with pytest.raises(
        DomainValidationError,
        match="estimated_total_minutes must equal the session-minute total",
    ):
        replace(plan, estimated_total_minutes=plan.estimated_total_minutes + 1)


@pytest.mark.parametrize("revision", [0, -1, True])
def test_revision_starts_at_one(revision: int) -> None:
    plan = make_plan()

    with pytest.raises(DomainValidationError, match="revision must start at 1"):
        replace(plan, revision=revision)


@pytest.mark.parametrize("version", [0, -1, True])
def test_plan_requires_positive_version(version: int) -> None:
    with pytest.raises(DomainValidationError, match="positive integer"):
        make_plan(version=version)


def test_confirm_transitions_validated_plan_and_increments_version() -> None:
    plan = make_plan(status=WeeklyPlanStatus.VALIDATED, version=3)
    confirmed_at = TEST_NOW + timedelta(hours=1)

    confirmed = plan.confirm(expected_version=3, confirmed_at=confirmed_at)

    assert plan.status is WeeklyPlanStatus.VALIDATED
    assert plan.version == 3
    assert confirmed.status is WeeklyPlanStatus.CONFIRMED
    assert confirmed.version == 4
    assert confirmed.confirmed_at == confirmed_at
    assert confirmed.updated_at == confirmed_at


def test_confirm_rejects_stale_expected_version() -> None:
    plan = make_plan(status=WeeklyPlanStatus.VALIDATED, version=2)

    with pytest.raises(DomainConflictError, match="current version is 2"):
        plan.confirm(expected_version=1, confirmed_at=TEST_NOW)


@pytest.mark.parametrize(
    "status",
    [WeeklyPlanStatus.DRAFT, WeeklyPlanStatus.CONFIRMED, WeeklyPlanStatus.CANCELLED],
)
def test_only_validated_plan_can_be_confirmed(status: WeeklyPlanStatus) -> None:
    plan = make_plan(status=status)

    with pytest.raises(
        InvalidDomainStateTransition,
        match="only a VALIDATED plan can be confirmed",
    ):
        plan.confirm(expected_version=plan.version, confirmed_at=TEST_NOW)


def test_confirmed_at_is_reserved_for_confirmed_state() -> None:
    plan = make_plan()

    with pytest.raises(
        DomainValidationError,
        match="confirmed_at is only valid for CONFIRMED plans",
    ):
        replace(plan, confirmed_at=TEST_NOW)


def test_plan_owns_mutable_snapshot_inputs() -> None:
    plan = make_plan()
    source_goal = {"primary_goal": "GENERAL_FITNESS", "nested": {"level": 1}}
    source_constraints = ({"type": "EXCLUDED_FEATURE", "values": ["jumping"]},)

    owned = replace(
        plan,
        goal_snapshot=source_goal,
        constraint_snapshot=source_constraints,
    )
    source_goal["nested"]["level"] = 99
    source_constraints[0]["values"].append("running")

    assert owned.goal_snapshot["nested"] == {"level": 1}
    assert owned.constraint_snapshot[0]["values"] == ["jumping"]
    assert owned.week_start == TEST_WEEK_START
