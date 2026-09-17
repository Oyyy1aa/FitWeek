"""Phase 7B behavior proposal import remains evidence-gated and review-only."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.domain.behavior.enums import (
    BehaviorConfidenceTier,
    BehaviorMemoryProposalStatus,
)
from app.domain.behavior.models import BehaviorMemoryProposal, BehaviorSummary
from app.domain.memory.enums import MemorySource, MemoryStatus, MemoryType
from app.domain.memory.models import UserMemory
from app.recovery_application.memory_policy import (
    BehaviorMemoryProposalImportPolicy,
)

pytestmark = pytest.mark.phase_7b

NOW = datetime(2026, 7, 21, 8, tzinfo=UTC)


def _summary(user_id: UUID, *, conflicts: tuple[UUID, ...] = ()) -> BehaviorSummary:
    return BehaviorSummary(
        id=uuid4(),
        user_id=user_id,
        window_start_utc=NOW - timedelta(days=28),
        window_end_utc=NOW,
        timezone="UTC",
        scheduled_session_count=3,
        checked_in_session_count=3,
        completed_count=3,
        partially_completed_count=0,
        skipped_count=0,
        missing_checkin_count=0,
        completion_rate=Decimal("1.0000"),
        participation_rate=Decimal("1.0000"),
        rpe_sample_count=0,
        average_reported_rpe=None,
        high_reported_rpe_count=0,
        repeated_time_patterns=(),
        repeated_location_patterns=(),
        repeated_skip_patterns=(),
        evidence_references=(),
        conflict_checkin_ids=tuple(sorted(conflicts, key=str)),
        policy_version="behavior-summary-policy-v1",
        fingerprint="a" * 64,
        created_at=NOW,
    )


def _proposal(user_id: UUID, evidence: tuple[UUID, ...]) -> BehaviorMemoryProposal:
    return BehaviorMemoryProposal(
        id=uuid4(),
        user_id=user_id,
        memory_type=MemoryType.PREFERRED_TIME_OF_DAY,
        proposed_key="preferred_time_of_day",
        proposed_value="MORNING",
        behavior_pattern_ids=("repeated-morning",),
        evidence_checkin_ids=tuple(sorted(evidence, key=str)),
        confidence_tier=BehaviorConfidenceTier.STRONG,
        status=BehaviorMemoryProposalStatus.PROPOSED,
        created_at=NOW,
        expires_at=NOW + timedelta(days=30),
    )


def _active_memory(user_id: UUID, value: str) -> UserMemory:
    return UserMemory(
        id=uuid4(),
        user_id=user_id,
        memory_type=MemoryType.PREFERRED_TIME_OF_DAY,
        key="preferred_time_of_day",
        normalized_value=value,
        display_value=value,
        status=MemoryStatus.ACTIVE,
        source=MemorySource.BEHAVIOR_CANDIDATE,
        confidence=Decimal("0.9"),
        valid_from=NOW,
        valid_until=None,
        confirmed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=None,
        version=1,
    )


def test_importable_proposal_requires_thresholded_non_conflicting_evidence() -> None:
    user_id = uuid4()
    evidence = tuple(uuid4() for _ in range(3))
    item = BehaviorMemoryProposalImportPolicy().evaluate(
        proposal=_proposal(user_id, evidence),
        summary=_summary(user_id),
        active_memories=(),
        now=NOW,
    )
    assert item.importable is True
    assert item.reason_codes == ()


def test_conflicting_checkin_and_active_memory_block_import() -> None:
    user_id = uuid4()
    evidence = tuple(uuid4() for _ in range(3))
    item = BehaviorMemoryProposalImportPolicy().evaluate(
        proposal=_proposal(user_id, evidence),
        summary=_summary(user_id, conflicts=(evidence[0],)),
        active_memories=(_active_memory(user_id, "EVENING"),),
        now=NOW,
    )
    assert item.importable is False
    assert set(item.reason_codes) == {
        "ACTIVE_MEMORY_CONFLICT",
        "CONFLICTING_CHECK_IN_EVIDENCE",
    }


def test_temporary_or_under_threshold_proposal_cannot_be_imported() -> None:
    user_id = uuid4()
    evidence = (uuid4(), uuid4())
    proposal = replace(
        _proposal(user_id, evidence),
        status=BehaviorMemoryProposalStatus.DISMISSED,
    )
    item = BehaviorMemoryProposalImportPolicy().evaluate(
        proposal=proposal,
        summary=_summary(user_id),
        active_memories=(),
        now=NOW,
    )
    assert item.importable is False
    assert set(item.reason_codes) == {
        "INSUFFICIENT_BEHAVIOR_EVIDENCE",
        "PROPOSAL_NOT_ACTIVE",
    }
