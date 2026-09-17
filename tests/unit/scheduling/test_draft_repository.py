from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.common import DomainValidationError, LocationType
from app.domain.context.enums import ContextDegradedMode
from app.domain.scheduling.enums import (
    CalendarReadMode,
    CalendarVerificationStatus,
    ScheduleDraftOutcome,
    ScheduleDraftSource,
    ScheduleDraftStatus,
)
from app.domain.scheduling.models import (
    BusySnapshot,
    ScheduleAssignment,
    ScheduleDraft,
    ScheduleTrace,
    TimeSlotCandidateSet,
    UnresolvedSession,
)
from app.persistence.memory.schedule_repository import InMemoryScheduleDraftRepository
from app.persistence.memory.store import InMemoryStore

pytestmark = pytest.mark.phase_6a
NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _values(partial: bool = False):
    user_id, draft_id, request_id, plan_id = uuid4(), uuid4(), uuid4(), uuid4()
    busy = BusySnapshot(
        id=uuid4(),
        user_id=user_id,
        timezone="UTC",
        range_start_utc=NOW,
        range_end_utc=NOW + timedelta(days=7),
        mode=CalendarReadMode.DISABLED,
        verification_status=CalendarVerificationStatus.MANUAL_ONLY,
        intervals=(),
        fingerprint="b" * 64,
        provider_summary="disabled",
        provider_name="disabled",
        provider_version="none",
        created_at=NOW,
    )
    candidate = TimeSlotCandidateSet(
        id=uuid4(),
        user_id=user_id,
        root_plan_id=plan_id,
        source_revision=1,
        busy_snapshot_id=busy.id,
        context_snapshot_reference_id=uuid4(),
        slots=(),
        session_candidates=(),
        fingerprint="c" * 64,
        policy_version="schedule-candidate-set-v1",
        created_at=NOW,
    )
    session_id = uuid4()
    unresolved = (
        (UnresolvedSession(session_id=session_id, code="NO_SLOT", message="No Slot."),)
        if partial
        else ()
    )
    assignments = (
        ()
        if partial
        else (
            ScheduleAssignment(
                session_id=session_id,
                slot_id="slot-1",
                scheduled_start=NOW + timedelta(days=1),
                scheduled_end=NOW + timedelta(days=1, minutes=30),
                location=LocationType.HOME,
            ),
        )
    )
    draft = ScheduleDraft(
        id=draft_id,
        request_id=request_id,
        client_request_id="request-1",
        user_id=user_id,
        request_payload_fingerprint="p" * 64,
        request_fingerprint="r" * 64,
        root_plan_id=plan_id,
        source_revision=1,
        source_plan_version=2,
        timezone="UTC",
        busy_snapshot_id=busy.id,
        candidate_set_id=candidate.id,
        candidate_set_fingerprint=candidate.fingerprint,
        context_snapshot_reference_id=candidate.context_snapshot_reference_id,
        context_fingerprint="x" * 64,
        context_degraded_mode=ContextDegradedMode.NONE,
        assignments=assignments,
        unresolved=unresolved,
        outcome=ScheduleDraftOutcome.PARTIAL
        if partial
        else ScheduleDraftOutcome.COMPLETE,
        source=ScheduleDraftSource.DETERMINISTIC_FALLBACK,
        prompt_version="schedule-agent-v1",
        provider_summary="fallback",
        fallback_used=True,
        calendar_verification_status=busy.verification_status,
        explanation_summary="safe",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    trace = ScheduleTrace(
        draft_id=draft.id,
        request_id=request_id,
        busy_snapshot_id=busy.id,
        candidate_set_id=candidate.id,
        candidate_set_fingerprint=candidate.fingerprint,
        context_snapshot_reference_id=candidate.context_snapshot_reference_id,
        context_fingerprint="x" * 64,
        prompt_version="schedule-agent-v1",
        provider_summary="fallback",
        timezone="UTC",
        provider_name="fallback",
        provider_version="v1",
        attempt_no=0,
        outcome="FALLBACK",
        validation_error_code=None,
        latency_ms=0,
        source=ScheduleDraftSource.DETERMINISTIC_FALLBACK,
        fallback_used=True,
        provider_attempts=0,
        calendar_mode=CalendarReadMode.DISABLED,
        calendar_attempts=0,
        model_trace_ids=(),
        created_at=NOW,
    )
    return draft, busy, candidate, trace


def test_partial_draft_cannot_be_accepted() -> None:
    draft, *_ = _values(partial=True)
    with pytest.raises(DomainValidationError):
        draft.accept(NOW + timedelta(minutes=1))


def test_complete_draft_accepts_once() -> None:
    draft, *_ = _values()
    accepted = draft.accept(NOW + timedelta(minutes=1))
    assert accepted.status is ScheduleDraftStatus.ACCEPTED
    assert accepted.version == 2
    with pytest.raises(DomainValidationError):
        accepted.reject(NOW + timedelta(minutes=2))


@pytest.mark.asyncio
async def test_repository_is_user_scoped_and_resettable() -> None:
    draft, busy, candidate, trace = _values()
    repository = InMemoryScheduleDraftRepository(InMemoryStore())
    await repository.save(draft, busy, candidate, trace)
    assert await repository.get_draft(draft.user_id, draft.id) == draft
    assert await repository.get_draft(uuid4(), draft.id) is None
    assert await repository.get_busy_snapshot(draft.user_id, draft.id) == busy
    await repository.clear()
    assert await repository.get_draft(draft.user_id, draft.id) is None
