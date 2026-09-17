import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.common import LocationType
from app.domain.scheduling.models import (
    SessionSlotCandidates,
    TimeSlotCandidate,
    TimeSlotCandidateSet,
)
from app.scheduling.fallback import DeterministicScheduleFallback
from app.scheduling.validator import ScheduleAgentBusinessValidator

pytestmark = pytest.mark.phase_6a

CASES = json.loads(
    (Path(__file__).with_name("schedule_agent_cases.json")).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=[item["id"] for item in CASES])
def test_72_controlled_schedule_contract_cases(case: dict[str, object]) -> None:
    count = int(case["session_count"])
    overlap = case["mode"] == "overlap"
    base = datetime(2026, 7, 20, 10, tzinfo=UTC)
    slots = []
    groups = []
    for index in range(count):
        session_id = uuid4()
        slot_id = f"slot-{index:02d}"
        start = base if overlap else base + timedelta(hours=index)
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
    candidate_set = TimeSlotCandidateSet(
        id=uuid4(),
        user_id=uuid4(),
        root_plan_id=uuid4(),
        source_revision=1,
        busy_snapshot_id=uuid4(),
        context_snapshot_reference_id=uuid4(),
        slots=tuple(slots),
        session_candidates=tuple(groups),
        fingerprint="a" * 64,
        policy_version="schedule-candidate-set-v1",
        created_at=base,
    )
    result = DeterministicScheduleFallback().build(candidate_set)
    ScheduleAgentBusinessValidator().validate(result, candidate_set)
    assert len(result.assignments) == int(case["expected"])
    assert len(result.assignments) + len(result.unresolved_session_ids) == count
