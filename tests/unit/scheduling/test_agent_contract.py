from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.common import LocationType
from app.domain.scheduling.models import (
    ScheduleAgentOutput,
    ScheduleAssignmentOutput,
    SessionSlotCandidates,
    TimeSlotCandidate,
    TimeSlotCandidateSet,
)
from app.scheduling.fallback import DeterministicScheduleFallback
from app.scheduling.validator import ScheduleAgentBusinessValidator

pytestmark = pytest.mark.phase_6a


def candidate_set(overlap: bool = False) -> TimeSlotCandidateSet:
    user_id, plan_id, context_id, busy_id = uuid4(), uuid4(), uuid4(), uuid4()
    sessions = (uuid4(), uuid4())
    base = datetime(2026, 7, 20, 10, tzinfo=UTC)
    slots = []
    groups = []
    for index, session_id in enumerate(sessions):
        start = base if overlap else base + timedelta(hours=index)
        slot_id = f"slot-{index}"
        slots.append(
            TimeSlotCandidate(
                session_id=session_id,
                slot_id=slot_id,
                start=start,
                end=start + timedelta(minutes=30),
                location=LocationType.HOME,
            )
        )
        groups.append(SessionSlotCandidates(session_id=session_id, slot_ids=(slot_id,)))
    return TimeSlotCandidateSet(
        id=uuid4(),
        user_id=user_id,
        root_plan_id=plan_id,
        source_revision=1,
        busy_snapshot_id=busy_id,
        context_snapshot_reference_id=context_id,
        slots=tuple(slots),
        session_candidates=tuple(groups),
        fingerprint="f" * 64,
        policy_version="schedule-candidate-set-v1",
        created_at=datetime(2026, 7, 19, tzinfo=UTC),
    )


def test_fallback_is_deterministic_and_complete() -> None:
    value = candidate_set()
    first = DeterministicScheduleFallback().build(value)
    second = DeterministicScheduleFallback().build(value)
    assert first == second
    assert len(first.assignments) == 2
    assert first.unresolved_session_ids == ()


def test_fallback_maximizes_assignment_and_returns_partial() -> None:
    result = DeterministicScheduleFallback().build(candidate_set(overlap=True))
    assert len(result.assignments) == 1
    assert len(result.unresolved_session_ids) == 1


def test_validator_rejects_unknown_slot() -> None:
    value = candidate_set()
    output = ScheduleAgentOutput(
        assignments=(
            ScheduleAssignmentOutput(
                session_id=value.session_candidates[0].session_id, slot_id="invented"
            ),
        ),
        unresolved_session_ids=(value.session_candidates[1].session_id,),
        explanation_summary="invalid",
    )
    with pytest.raises(ValueError, match="unknown"):
        ScheduleAgentBusinessValidator().validate(output, value)


def test_output_forbids_arbitrary_timestamp() -> None:
    with pytest.raises(ValidationError):
        ScheduleAgentOutput.model_validate(
            {
                "assignments": [],
                "unresolved_session_ids": [],
                "explanation_summary": "x",
                "scheduled_start": "2026-01-01T00:00:00Z",
            }
        )
