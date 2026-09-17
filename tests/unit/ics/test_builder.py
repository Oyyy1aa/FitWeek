"""RFC 5545 subset contracts for deterministic FitWeek exports."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.domain.plans.models import WeeklyPlanStatus
from app.domain.sessions.models import WorkoutSessionStatus
from app.ics.builder import IcsBuilder
from app.ics.escaping import escape_text
from app.ics.folding import fold_line
from app.ics.validator import validate_ics
from tests.factories import make_plan

pytestmark = pytest.mark.phase_6b


def test_text_escaping_and_utf8_octet_folding() -> None:
    assert escape_text("a,b;c\\d\r\ne") == "a\\,b\\;c\\\\d\\ne"
    logical = "DESCRIPTION:" + "训练安排，" * 30
    physical = fold_line(logical)
    assert len(physical) > 1
    assert all(len(line.encode("utf-8")) <= 75 for line in physical)
    assert all(line.startswith(" ") for line in physical[1:])
    assert physical[0] + "".join(line[1:] for line in physical[1:]) == logical


def test_builder_is_deterministic_crlf_safe_and_excludes_cancelled() -> None:
    plan = make_plan(status=WeeklyPlanStatus.CONFIRMED)
    plan = replace(
        plan,
        sessions=(
            plan.sessions[0],
            replace(plan.sessions[1], status=WorkoutSessionStatus.CANCELLED),
        ),
    )
    builder = IcsBuilder()
    as_of = datetime(2026, 7, 19, tzinfo=UTC)
    first, count = builder.build(user_id=plan.user_id, plan=plan, as_of=as_of)
    second, second_count = builder.build(user_id=plan.user_id, plan=plan, as_of=as_of)
    assert first == second
    assert count == second_count == 1
    assert first.count(b"BEGIN:VEVENT") == 1
    assert b"\n" not in first.replace(b"\r\n", b"")
    assert b"SUMMARY:FitWeek Training" in first
    assert b"dev-user" not in first
    validate_ics(first)


def test_validator_rejects_lf_and_oversized_physical_line() -> None:
    with pytest.raises(ValueError, match="CRLF"):
        validate_ics(
            b"BEGIN:VCALENDAR\nVERSION:2.0\nCALSCALE:GREGORIAN\nEND:VCALENDAR\n"
        )
    oversized = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nCALSCALE:GREGORIAN\r\n"
        f"DESCRIPTION:{'x' * 80}\r\nEND:VCALENDAR\r\n"
    ).encode()
    with pytest.raises(ValueError, match="75 octets"):
        validate_ics(oversized)
