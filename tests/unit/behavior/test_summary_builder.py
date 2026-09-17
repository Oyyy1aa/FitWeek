"""Phase 7A deterministic behavior-summary policy tests."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.behavior.metrics import RecoveryMetrics
from app.behavior.summary_builder import BehaviorSummaryBuilder
from app.domain.behavior.enums import BehaviorPatternType
from app.domain.behavior.models import BehaviorSummaryWindow
from app.domain.checkins.models import CheckInStatus, WorkoutCheckIn
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.orchestration.clock import FakeClock
from tests.factories import make_plan

pytestmark = pytest.mark.phase_7a

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)


def _builder(**changes: object) -> BehaviorSummaryBuilder:
    values = {
        "clock": FakeClock(NOW),
        "metrics": RecoveryMetrics(),
        "default_window_days": 28,
        "max_window_days": 56,
        "min_signal_occurrences": 3,
        "repeat_ratio_threshold": Decimal("0.60"),
        "min_rpe_samples": 2,
    }
    values.update(changes)
    return BehaviorSummaryBuilder(**values)  # type: ignore[arg-type]


def _past_plan(*, user_id: object | None = None, count: int = 4) -> WeeklyPlan:
    plan = make_plan(
        user_id=user_id,  # type: ignore[arg-type]
        week_start=date(2026, 7, 13),
        session_count=count,
        status=WeeklyPlanStatus.CONFIRMED,
        version=2,
    )
    sessions = tuple(
        replace(
            item,
            scheduled_start=datetime(2026, 7, 13, 8, tzinfo=UTC)
            + timedelta(days=index),
            scheduled_end=datetime(2026, 7, 13, 8, 30, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, item in enumerate(plan.sessions)
    )
    return replace(
        plan,
        sessions=sessions,
        estimated_total_minutes=sum(item.estimated_minutes for item in sessions),
    )


def _check_in(
    plan: WeeklyPlan,
    index: int,
    status: CheckInStatus,
    *,
    rpe: int | None = None,
    checkin_id: object | None = None,
) -> WorkoutCheckIn:
    session = plan.sessions[index]
    at = session.scheduled_end
    return WorkoutCheckIn(
        id=checkin_id or uuid4(),  # type: ignore[arg-type]
        client_event_id=f"event-{index}-{status.value}",
        user_id=plan.user_id,
        plan_id=plan.series_id,
        plan_revision=plan.revision,
        session_id=session.id,
        status=status,
        actual_minutes=0 if status is CheckInStatus.SKIPPED else 25,
        perceived_effort=rpe,
        note="PRIVATE NOTE MUST NEVER APPEAR",
        occurred_at=at,
        created_at=at,
        updated_at=at,
        version=1,
    )


def test_transparent_metrics_missing_and_rpe_threshold() -> None:
    user_id = uuid4()
    plan = _past_plan(user_id=user_id)
    check_ins = (
        _check_in(plan, 0, CheckInStatus.COMPLETED, rpe=7),
        _check_in(plan, 1, CheckInStatus.PARTIALLY_COMPLETED, rpe=9),
        _check_in(plan, 2, CheckInStatus.SKIPPED),
    )
    summary = _builder().build(
        user_id=user_id,
        timezone="UTC",
        plans=(plan,),
        check_ins=check_ins,
    )
    assert summary.scheduled_session_count == 4
    assert summary.checked_in_session_count == 3
    assert summary.completed_count == 1
    assert summary.partially_completed_count == 1
    assert summary.skipped_count == 1
    assert summary.missing_checkin_count == 1
    assert summary.completion_rate == Decimal("0.2500")
    assert summary.participation_rate == Decimal("0.5000")
    assert summary.average_reported_rpe == Decimal("8.0000")
    assert all("PRIVATE" not in repr(item) for item in summary.evidence_references)


def test_default_max_window_future_and_timezone_boundaries() -> None:
    user_id = uuid4()
    plan = _past_plan(user_id=user_id)
    summary = _builder().build(
        user_id=user_id,
        timezone="Asia/Shanghai",
        plans=(plan,),
        check_ins=(),
    )
    assert summary.window_end_utc == datetime(2026, 7, 21, 16, tzinfo=UTC)
    assert summary.window_end_utc - summary.window_start_utc == timedelta(days=28)
    with pytest.raises(ValueError, match="56"):
        _builder().build(
            user_id=user_id,
            timezone="UTC",
            plans=(plan,),
            check_ins=(),
            window=BehaviorSummaryWindow(
                start_date=date(2026, 5, 1), end_date=date(2026, 7, 1)
            ),
        )


def test_revision_lineage_and_conflicting_checkins_are_deduplicated() -> None:
    user_id = uuid4()
    plan = _past_plan(user_id=user_id, count=2)
    first = _check_in(plan, 0, CheckInStatus.COMPLETED)
    conflict = replace(
        first,
        id=uuid4(),
        client_event_id="conflict",
        status=CheckInStatus.SKIPPED,
        actual_minutes=0,
        created_at=first.created_at + timedelta(seconds=1),
        updated_at=first.updated_at + timedelta(seconds=1),
    )
    summary = _builder().build(
        user_id=user_id,
        timezone="UTC",
        plans=(plan, plan),
        check_ins=(first, conflict),
    )
    assert summary.scheduled_session_count == 2
    assert summary.completed_count == 1
    assert summary.skipped_count == 0
    assert set(summary.conflict_checkin_ids) == {first.id, conflict.id}


def test_pattern_threshold_and_stable_fingerprint() -> None:
    user_id = uuid4()
    plan = _past_plan(user_id=user_id, count=4)
    sessions = tuple(
        replace(
            item,
            scheduled_start=datetime(2026, 7, 13, 8, tzinfo=UTC)
            + timedelta(days=7 * index),
            scheduled_end=datetime(2026, 7, 13, 8, 30, tzinfo=UTC)
            + timedelta(days=7 * index),
        )
        for index, item in enumerate(plan.sessions)
    )
    plan = replace(
        plan,
        sessions=sessions,
        estimated_total_minutes=sum(item.estimated_minutes for item in sessions),
    )
    # Use an expanded clock/window so all four Mondays are historical.
    builder = BehaviorSummaryBuilder(
        clock=FakeClock(datetime(2026, 8, 15, tzinfo=UTC)),
        metrics=RecoveryMetrics(),
        default_window_days=56,
        max_window_days=56,
        min_signal_occurrences=3,
        repeat_ratio_threshold=Decimal("0.60"),
        min_rpe_samples=2,
    )
    checks = tuple(_check_in(plan, index, CheckInStatus.SKIPPED) for index in range(3))
    first = builder.build(
        user_id=user_id, timezone="UTC", plans=(plan,), check_ins=checks
    )
    second = builder.build(
        user_id=user_id, timezone="UTC", plans=(plan,), check_ins=checks
    )
    assert first.fingerprint == second.fingerprint
    assert any(
        item.pattern_type is BehaviorPatternType.REPEATED_SKIP_WEEKDAY
        for item in first.repeated_skip_patterns
    )


def test_user_and_root_plan_lineage_are_isolated() -> None:
    user_id = uuid4()
    first = _past_plan(user_id=user_id, count=2)
    second = _past_plan(user_id=user_id, count=2)
    second = replace(
        second,
        sessions=(
            replace(
                second.sessions[0],
                id=first.sessions[0].id,
                plan_id=second.series_id,
            ),
            second.sessions[1],
        ),
    )
    other = _past_plan(user_id=uuid4(), count=2)
    summary = _builder().build(
        user_id=user_id,
        timezone="UTC",
        plans=(first, second, other),
        check_ins=(
            _check_in(first, 0, CheckInStatus.COMPLETED),
            _check_in(second, 0, CheckInStatus.SKIPPED),
            _check_in(other, 0, CheckInStatus.COMPLETED),
        ),
    )
    assert summary.scheduled_session_count == 4
    assert summary.checked_in_session_count == 2
    assert summary.completed_count == 1
    assert summary.skipped_count == 1


def test_one_rpe_sample_is_reported_but_not_averaged() -> None:
    user_id = uuid4()
    plan = _past_plan(user_id=user_id, count=2)
    summary = _builder().build(
        user_id=user_id,
        timezone="UTC",
        plans=(plan,),
        check_ins=(_check_in(plan, 0, CheckInStatus.COMPLETED, rpe=9),),
    )
    assert summary.rpe_sample_count == 1
    assert summary.average_reported_rpe is None
    assert summary.high_reported_rpe_count == 1
