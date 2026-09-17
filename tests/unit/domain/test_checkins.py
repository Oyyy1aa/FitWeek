"""Intrinsic check-in invariants."""

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

import pytest

from app.domain.checkins.models import CheckInStatus
from app.domain.common import DomainValidationError
from tests.phase1a2_helpers import make_check_in

pytestmark = pytest.mark.phase_1a2


@pytest.mark.parametrize(
    ("status", "actual"),
    [
        (CheckInStatus.COMPLETED, 30),
        (CheckInStatus.PARTIALLY_COMPLETED, 10),
        (CheckInStatus.SKIPPED, None),
    ],
)
def test_supported_check_in_statuses_are_valid(
    status: CheckInStatus, actual: int | None
) -> None:
    assert make_check_in(status=status, actual_minutes=actual).status is status


@pytest.mark.parametrize(
    "changes",
    [
        {"status": CheckInStatus.COMPLETED, "actual_minutes": None},
        {"status": CheckInStatus.PARTIALLY_COMPLETED, "actual_minutes": 0},
        {"status": CheckInStatus.SKIPPED, "actual_minutes": 1},
        {"perceived_effort": 11},
        {"occurred_at": datetime(2026, 7, 20, 11)},
        {"note": "x" * 501},
        {"version": 0},
        {"client_event_id": " "},
    ],
)
def test_invalid_check_in_values_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(DomainValidationError):
        make_check_in(**changes)


def test_idempotent_payload_comparison_ignores_server_identity() -> None:
    first = make_check_in()
    second = replace(first, id=uuid4(), version=2)

    assert first.same_event_payload(second) is True
