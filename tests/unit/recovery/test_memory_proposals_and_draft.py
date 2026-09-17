"""Thresholded Proposal and review-only Draft lifecycle tests."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.behavior.metrics import RecoveryMetrics
from app.domain.behavior.enums import BehaviorPatternType
from app.domain.behavior.models import BehaviorPattern, BehaviorSummary
from app.domain.memory.enums import MemoryType
from app.domain.recovery.enums import (
    RecoveryDraftOutcome,
    RecoveryDraftSource,
    RecoveryDraftStatus,
    RecoveryScopeStatus,
)
from app.domain.recovery.models import RecoveryDraft
from app.orchestration.clock import FakeClock
from app.recovery.memory_proposals import BehaviorMemoryProposalBuilder

pytestmark = pytest.mark.phase_7a

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)


def _summary(user_id: object, pattern: BehaviorPattern) -> BehaviorSummary:
    return BehaviorSummary(
        id=uuid4(),
        user_id=user_id,  # type: ignore[arg-type]
        window_start_utc=NOW - timedelta(days=28),
        window_end_utc=NOW,
        timezone="UTC",
        scheduled_session_count=3,
        checked_in_session_count=3,
        completed_count=0,
        partially_completed_count=0,
        skipped_count=3,
        missing_checkin_count=0,
        completion_rate=Decimal("0.0000"),
        participation_rate=Decimal("0.0000"),
        rpe_sample_count=0,
        average_reported_rpe=None,
        high_reported_rpe_count=0,
        repeated_time_patterns=(pattern,),
        repeated_location_patterns=(),
        repeated_skip_patterns=(pattern,),
        evidence_references=(),
        conflict_checkin_ids=(),
        policy_version="behavior-summary-policy-v1",
        fingerprint="a" * 64,
        created_at=NOW,
    )


def test_behavior_proposal_requires_pattern_and_temporary_request_blocks() -> None:
    user_id = uuid4()
    evidence_ids = tuple(uuid4() for _ in range(3))
    pattern = BehaviorPattern(
        pattern_id="pattern-monday-morning",
        pattern_type=BehaviorPatternType.REPEATED_SKIP_TIME_OF_DAY,
        key="MORNING",
        occurrence_count=3,
        opportunity_count=4,
        ratio=Decimal("0.7500"),
        evidence_ids=tuple(sorted(evidence_ids, key=str)),
    )
    metrics = RecoveryMetrics()
    builder = BehaviorMemoryProposalBuilder(FakeClock(NOW), metrics)
    proposals = builder.build(
        user_id=user_id,
        summary=_summary(user_id, pattern),
        user_request="review",
        proposal_scope="request-1",
    )
    assert len(proposals) == 1
    assert proposals[0].memory_type is MemoryType.PREFERRED_TIME_OF_DAY
    assert set(proposals[0].evidence_checkin_ids) == set(evidence_ids)
    assert (
        builder.build(
            user_id=user_id,
            summary=_summary(user_id, pattern),
            proposal_scope="request-1",
            user_request="只是本周临时有事",
        )
        == ()
    )


def test_behavior_proposal_ids_are_stable_per_request_and_distinct_across_requests() -> (  # noqa: E501
    None
):
    user_id = uuid4()
    evidence_ids = tuple(sorted((uuid4(), uuid4(), uuid4()), key=str))
    pattern = BehaviorPattern(
        pattern_id="request-scoped-morning",
        pattern_type=BehaviorPatternType.REPEATED_SKIP_TIME_OF_DAY,
        key="MORNING",
        occurrence_count=3,
        opportunity_count=4,
        ratio=Decimal("0.7500"),
        evidence_ids=evidence_ids,
    )
    builder = BehaviorMemoryProposalBuilder(
        FakeClock(NOW),
        RecoveryMetrics(),
    )
    summary = _summary(user_id, pattern)
    first = builder.build(
        user_id=user_id,
        summary=summary,
        user_request="review",
        proposal_scope="request-a",
    )
    replay = builder.build(
        user_id=user_id,
        summary=summary,
        user_request="review",
        proposal_scope="request-a",
    )
    second = builder.build(
        user_id=user_id,
        summary=summary,
        user_request="review",
        proposal_scope="request-b",
    )

    assert first == replay
    assert len(first) == len(second) == 1
    assert first[0].id != second[0].id
    assert replace(second[0], id=first[0].id) == first[0]
    assert (
        builder.build(
            user_id=user_id,
            summary=summary,
            user_request="temporary this week",
            proposal_scope="request-b",
        )
        == ()
    )


def _draft(
    outcome: RecoveryDraftOutcome = RecoveryDraftOutcome.COMPLETE,
) -> RecoveryDraft:
    return RecoveryDraft(
        id=uuid4(),
        request_id=uuid4(),
        client_request_id="request-1",
        user_id=uuid4(),
        request_payload_fingerprint="a" * 64,
        request_fingerprint="b" * 64,
        root_plan_id=uuid4(),
        source_revision=1,
        source_plan_version=2,
        behavior_summary_id=uuid4(),
        context_snapshot_reference_id=uuid4(),
        change_impact_snapshot_id=uuid4(),
        candidate_set_id=uuid4(),
        selected_action_candidate_ids=(),
        unresolved_session_ids=(),
        behavior_memory_proposal_ids=(),
        explanation_summary="Controlled proposal for review.",
        outcome=outcome,
        source=RecoveryDraftSource.DETERMINISTIC_FALLBACK,
        fallback_used=True,
        scope_status=RecoveryScopeStatus.SUPPORTED,
        status=RecoveryDraftStatus.PENDING_REVIEW,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
        version=1,
    )


def test_draft_accept_reject_version_and_terminal_rules() -> None:
    accepted = _draft().accept(expected_version=1, at=NOW + timedelta(minutes=1))
    assert accepted.status is RecoveryDraftStatus.ACCEPTED
    assert accepted.version == 2
    rejected = _draft().reject(expected_version=1, at=NOW + timedelta(minutes=1))
    assert rejected.status is RecoveryDraftStatus.REJECTED
    with pytest.raises(RuntimeError):
        accepted.reject(expected_version=2, at=NOW + timedelta(minutes=2))
    with pytest.raises(RuntimeError):
        _draft(RecoveryDraftOutcome.PARTIAL).accept(
            expected_version=1, at=NOW + timedelta(minutes=1)
        )
