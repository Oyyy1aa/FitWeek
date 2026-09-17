from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.common import DomainValidationError, LocationType
from app.domain.context.enums import AgentType, ContextDegradedMode
from app.domain.context.models import ContextSnapshotReference
from app.domain.scheduling.enums import BusyIntervalSource, CalendarReadMode
from app.domain.scheduling.models import AvailabilityWindow, BusyInterval
from app.scheduling.busy import build_busy_snapshot, merge_busy
from app.scheduling.candidate_set import TimeSlotCandidateSetBuilder
from app.scheduling.time_policy import TimezonePolicy, overlaps
from tests.factories import TEST_NOW, make_session

pytestmark = pytest.mark.phase_6a


def test_half_open_interval_policy() -> None:
    start = datetime(2026, 7, 20, 10, tzinfo=UTC)
    assert not overlaps(
        start,
        start + timedelta(hours=1),
        start + timedelta(hours=1),
        start + timedelta(hours=2),
    )
    assert overlaps(
        start,
        start + timedelta(hours=1),
        start + timedelta(minutes=59),
        start + timedelta(hours=2),
    )


def test_timezone_offset_must_match_iana_zone() -> None:
    policy = TimezonePolicy()
    value = datetime.fromisoformat("2026-07-20T10:00:00+00:00")
    with pytest.raises(DomainValidationError) as error:
        policy.to_utc(value, "Asia/Shanghai", "start")
    assert error.value.code == "TIMEZONE_OFFSET_MISMATCH"


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(DomainValidationError) as error:
        TimezonePolicy().zone("Mars/Olympus")
    assert error.value.code == "INVALID_TIMEZONE"


def test_nonexistent_dst_time_rejected() -> None:
    value = datetime.fromisoformat("2026-03-08T02:30:00-05:00")
    with pytest.raises(DomainValidationError) as error:
        TimezonePolicy().to_utc(value, "America/New_York", "start")
    assert error.value.code == "NONEXISTENT_LOCAL_TIME"


def test_ambiguous_dst_time_is_disambiguated_by_offset() -> None:
    first = TimezonePolicy().to_utc(
        datetime.fromisoformat("2026-11-01T01:30:00-04:00"),
        "America/New_York",
        "start",
    )
    second = TimezonePolicy().to_utc(
        datetime.fromisoformat("2026-11-01T01:30:00-05:00"),
        "America/New_York",
        "start",
    )
    assert second - first == timedelta(hours=1)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(DomainValidationError) as error:
        TimezonePolicy().to_utc(datetime(2026, 7, 20, 10), "UTC", "start")
    assert error.value.code == "NAIVE_DATETIME"


def test_busy_intervals_merge_without_content() -> None:
    start = datetime(2026, 7, 20, 10, tzinfo=UTC)
    merged = merge_busy(
        (
            BusyInterval(
                start=start,
                end=start + timedelta(hours=1),
                source=BusyIntervalSource.MANUAL,
            ),
            BusyInterval(
                start=start + timedelta(minutes=30),
                end=start + timedelta(hours=2),
                source=BusyIntervalSource.MANUAL,
            ),
        )
    )
    assert len(merged) == 1
    assert merged[0].end == start + timedelta(hours=2)


def _reference(user_id):
    return ContextSnapshotReference(
        id=uuid4(),
        user_id=user_id,
        agent_type=AgentType.SCHEDULE_AGENT,
        contract_version="schedule-agent-context-v1",
        policy_version="context-policy-v1",
        context_fingerprint="f" * 64,
        context_audit_id=uuid4(),
        profile_id=None,
        profile_version=None,
        constraint_versions=(),
        memory_versions=(),
        degraded_mode=ContextDegradedMode.NONE,
        created_at=TEST_NOW,
    )


def test_candidate_set_is_stable_and_excludes_busy() -> None:
    user_id, plan_id = uuid4(), uuid4()
    session = make_session(plan_id=plan_id, day_offset=0)
    window = AvailabilityWindow(
        start=session.scheduled_start,
        end=session.scheduled_start + timedelta(hours=2),
        location=LocationType.HOME,
    )
    busy = build_busy_snapshot(
        user_id=user_id,
        timezone="UTC",
        mode=CalendarReadMode.MANUAL_ONLY,
        provider_intervals=(),
        manual_intervals=(
            BusyInterval(
                start=session.scheduled_start,
                end=session.scheduled_start + timedelta(minutes=30),
                source=BusyIntervalSource.MANUAL,
            ),
        ),
        provider_summary="manual",
        created_at=TEST_NOW,
    )
    builder = TimeSlotCandidateSetBuilder(granularity_minutes=15)
    first = builder.build(
        user_id=user_id,
        root_plan_id=plan_id,
        source_revision=1,
        sessions=(session,),
        availability=(window,),
        busy=busy,
        immutable_sessions=(),
        context=_reference(user_id),
        created_at=TEST_NOW,
    )
    second = builder.build(
        user_id=user_id,
        root_plan_id=plan_id,
        source_revision=1,
        sessions=(session,),
        availability=(window,),
        busy=busy,
        immutable_sessions=(),
        context=_reference(user_id),
        created_at=TEST_NOW,
    )
    assert all(
        item.start >= session.scheduled_start + timedelta(minutes=30)
        for item in first.slots
    )
    assert tuple(item.slot_id for item in first.slots) == tuple(
        item.slot_id for item in second.slots
    )


def test_cross_midnight_availability_generates_utc_slots() -> None:
    user_id, plan_id = uuid4(), uuid4()
    start = datetime(2026, 7, 20, 23, 30, tzinfo=UTC)
    session = make_session(plan_id=plan_id, day_offset=0, start=start)
    busy = build_busy_snapshot(
        user_id=user_id,
        timezone="UTC",
        mode=CalendarReadMode.MANUAL_ONLY,
        provider_intervals=(),
        manual_intervals=(),
        provider_summary="manual",
        created_at=TEST_NOW,
    )
    candidates = TimeSlotCandidateSetBuilder(granularity_minutes=15).build(
        user_id=user_id,
        root_plan_id=plan_id,
        source_revision=1,
        sessions=(session,),
        availability=(
            AvailabilityWindow(
                start=start,
                end=start + timedelta(hours=1, minutes=30),
                location=LocationType.HOME,
            ),
        ),
        busy=busy,
        immutable_sessions=(),
        context=_reference(user_id),
        created_at=TEST_NOW,
    )
    assert any(item.start.date() != item.end.date() for item in candidates.slots)
    assert all(
        item.start.tzinfo is UTC and item.end.tzinfo is UTC for item in candidates.slots
    )


@pytest.mark.parametrize("blocked", [True, False])
def test_fully_busy_or_too_short_availability_has_no_candidate(
    blocked: bool,
) -> None:
    user_id, plan_id = uuid4(), uuid4()
    start = datetime(2026, 7, 20, 10, tzinfo=UTC)
    session = make_session(
        plan_id=plan_id,
        day_offset=0,
        start=start,
        estimated_minutes=45,
    )
    availability_end = (
        start + timedelta(hours=1) if blocked else start + timedelta(minutes=30)
    )
    manual_busy = (
        (
            BusyInterval(
                start=start,
                end=availability_end,
                source=BusyIntervalSource.MANUAL,
            ),
        )
        if blocked
        else ()
    )
    busy = build_busy_snapshot(
        user_id=user_id,
        timezone="UTC",
        mode=CalendarReadMode.MANUAL_ONLY,
        provider_intervals=(),
        manual_intervals=manual_busy,
        provider_summary="manual",
        created_at=TEST_NOW,
    )
    with pytest.raises(DomainValidationError) as error:
        TimeSlotCandidateSetBuilder(granularity_minutes=15).build(
            user_id=user_id,
            root_plan_id=plan_id,
            source_revision=1,
            sessions=(session,),
            availability=(
                AvailabilityWindow(
                    start=start,
                    end=availability_end,
                    location=LocationType.HOME,
                ),
            ),
            busy=busy,
            immutable_sessions=(),
            context=_reference(user_id),
            created_at=TEST_NOW,
        )
    assert error.value.code == "NO_VALID_TIME_SLOT"


def test_partial_busy_respects_half_open_boundaries() -> None:
    user_id, plan_id = uuid4(), uuid4()
    start = datetime(2026, 7, 20, 10, tzinfo=UTC)
    session = make_session(plan_id=plan_id, day_offset=0, start=start)
    busy = build_busy_snapshot(
        user_id=user_id,
        timezone="UTC",
        mode=CalendarReadMode.MANUAL_ONLY,
        provider_intervals=(),
        manual_intervals=(
            BusyInterval(
                start=start + timedelta(minutes=30),
                end=start + timedelta(hours=1),
                source=BusyIntervalSource.MANUAL,
            ),
        ),
        provider_summary="manual",
        created_at=TEST_NOW,
    )
    candidates = TimeSlotCandidateSetBuilder(granularity_minutes=15).build(
        user_id=user_id,
        root_plan_id=plan_id,
        source_revision=1,
        sessions=(session,),
        availability=(
            AvailabilityWindow(
                start=start,
                end=start + timedelta(hours=2),
                location=LocationType.HOME,
            ),
        ),
        busy=busy,
        immutable_sessions=(),
        context=_reference(user_id),
        created_at=TEST_NOW,
    )
    starts = {item.start for item in candidates.slots}
    assert start in starts
    assert start + timedelta(hours=1) in starts
    assert start + timedelta(minutes=15) not in starts
