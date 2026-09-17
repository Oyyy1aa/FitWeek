"""Intrinsic invariants for workout sessions and their exercise entries."""

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.common import DomainValidationError
from app.domain.sessions.models import SessionExercise
from tests.factories import TEST_NOW, make_session

pytestmark = pytest.mark.phase_1a


def _session_exercise(**overrides: object) -> SessionExercise:
    values: dict[str, object] = {
        "exercise_id": "bodyweight_squat",
        "sequence_no": 1,
        "sets": 2,
        "repetitions": 8,
        "duration_seconds": None,
        "rest_seconds": 30,
    }
    values.update(overrides)
    return SessionExercise(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("sequence_no", [0, -1, True])
def test_session_exercise_requires_positive_sequence(sequence_no: int) -> None:
    with pytest.raises(DomainValidationError, match="sequence_no must be at least 1"):
        _session_exercise(sequence_no=sequence_no)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("sets", 0),
        ("sets", -1),
        ("repetitions", 0),
        ("duration_seconds", 0),
        ("duration_seconds", True),
    ],
)
def test_session_exercise_requires_positive_prescription_values(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(DomainValidationError, match=f"{field_name} must be positive"):
        _session_exercise(**{field_name: value})


def test_session_exercise_requires_repetitions_or_duration() -> None:
    with pytest.raises(
        DomainValidationError,
        match="at least repetitions or duration_seconds must be provided",
    ):
        _session_exercise(repetitions=None, duration_seconds=None)


@pytest.mark.parametrize("rest_seconds", [-1, True])
def test_session_exercise_rejects_invalid_rest(rest_seconds: int) -> None:
    with pytest.raises(DomainValidationError, match="must not be negative"):
        _session_exercise(rest_seconds=rest_seconds)


def test_session_requires_datetime_values() -> None:
    session = make_session(plan_id=uuid4(), day_offset=0)

    with pytest.raises(DomainValidationError, match="must be a datetime"):
        replace(session, scheduled_end="2026-07-20T10:30:00Z")  # type: ignore[arg-type]


def test_session_requires_utc_aware_ordered_timestamps() -> None:
    session = make_session(plan_id=uuid4(), day_offset=0)

    with pytest.raises(DomainValidationError, match="timezone-aware"):
        replace(session, scheduled_start=TEST_NOW.replace(tzinfo=None))
    with pytest.raises(DomainValidationError, match="earlier than"):
        replace(session, scheduled_end=session.scheduled_start)


@pytest.mark.parametrize("minutes", [14, 61, True])
def test_session_requires_mvp_duration_range(minutes: int) -> None:
    session = make_session(plan_id=uuid4(), day_offset=0)

    with pytest.raises(DomainValidationError, match="between 15 and 60"):
        replace(session, estimated_minutes=minutes)


def test_session_requires_non_empty_unique_exercise_sequence() -> None:
    session = make_session(plan_id=uuid4(), day_offset=0)

    with pytest.raises(DomainValidationError, match="must not be empty"):
        replace(session, exercises=())

    duplicate = replace(session.exercises[0], exercise_id="wall_push_up")
    with pytest.raises(DomainValidationError, match="must be unique"):
        replace(session, exercises=(session.exercises[0], duplicate))


@pytest.mark.parametrize("version", [0, -1, True])
def test_session_requires_positive_version(version: int) -> None:
    session = make_session(plan_id=uuid4(), day_offset=0)

    with pytest.raises(DomainValidationError, match="positive integer"):
        replace(session, version=version)


def test_session_time_fixture_is_utc_and_matches_estimate() -> None:
    session = make_session(plan_id=uuid4(), day_offset=0, estimated_minutes=30)

    assert session.scheduled_start.utcoffset() == timedelta(0)
    assert session.scheduled_end - session.scheduled_start == timedelta(minutes=30)
    assert isinstance(session.scheduled_start, datetime)
