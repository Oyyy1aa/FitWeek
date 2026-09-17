"""MySQL round-trip contract for frozen Schedule inputs and review state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.domain.common import LocationType, RepositoryConflictError
from app.domain.context.enums import ContextDegradedMode
from app.domain.scheduling.enums import (
    BusyIntervalSource,
    CalendarReadMode,
    CalendarVerificationStatus,
    ScheduleDraftOutcome,
    ScheduleDraftSource,
)
from app.domain.scheduling.models import (
    AvailabilityWindow,
    BusyInterval,
    BusySnapshot,
    ScheduleAssignment,
    ScheduleDraft,
    ScheduleTrace,
    SessionSlotCandidates,
    TimeSlotCandidate,
    TimeSlotCandidateSet,
)
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AuditEventModel,
    ContextSnapshotModel,
    ScheduleApplicationResultModel,
    ScheduleAvailabilityWindowModel,
    ScheduleBusyIntervalModel,
    ScheduleBusySnapshotModel,
    ScheduleCandidateSetModel,
    ScheduleCandidateSlotModel,
    ScheduleDraftModel,
    ScheduleTraceModel,
    UserAccountModel,
)
from app.persistence.mysql.schedule_repository import MySQLScheduleDraftRepository


async def _seed_owner(
    database: Database, user_id: UUID, snapshot_id: UUID, now: datetime
) -> None:
    async with database.session_factory() as session:
        async with session.begin():
            session.add(
                UserAccountModel(
                    id=str(user_id),
                    email=f"schedule-repository-{user_id.hex}@fitweek.test",
                    display_name="Schedule Repository",
                    timezone="Asia/Shanghai",
                    status="ACTIVE",
                    created_at=now.replace(tzinfo=None),
                    updated_at=now.replace(tzinfo=None),
                    version=1,
                )
            )
            await session.flush()
            session.add(
                ContextSnapshotModel(
                    id=str(snapshot_id),
                    user_id=str(user_id),
                    agent_type="SCHEDULE_AGENT",
                    scope_id=f"schedule-repository-{snapshot_id}",
                    fingerprint="c" * 64,
                    contract_version="context-v1",
                    policy_version="policy-v1",
                    content={"safe_references": []},
                    character_count=0,
                    token_estimate=0,
                    created_at=now.replace(tzinfo=None),
                )
            )


async def _cleanup(database: Database, user_id: UUID) -> None:
    owner = str(user_id)
    async with database.session_factory() as session:
        async with session.begin():
            draft_ids = (
                await session.scalars(
                    select(ScheduleDraftModel.id).where(
                        ScheduleDraftModel.user_id == owner
                    )
                )
            ).all()
            candidate_ids = (
                await session.scalars(
                    select(ScheduleCandidateSetModel.id).where(
                        ScheduleCandidateSetModel.user_id == owner
                    )
                )
            ).all()
            snapshot_ids = (
                await session.scalars(
                    select(ScheduleBusySnapshotModel.id).where(
                        ScheduleBusySnapshotModel.user_id == owner
                    )
                )
            ).all()
            await session.execute(
                delete(ScheduleApplicationResultModel).where(
                    ScheduleApplicationResultModel.user_id == owner
                )
            )
            if draft_ids:
                await session.execute(
                    delete(ScheduleTraceModel).where(
                        ScheduleTraceModel.draft_id.in_(draft_ids)
                    )
                )
            await session.execute(
                delete(ScheduleDraftModel).where(ScheduleDraftModel.user_id == owner)
            )
            if candidate_ids:
                await session.execute(
                    delete(ScheduleCandidateSlotModel).where(
                        ScheduleCandidateSlotModel.candidate_set_id.in_(candidate_ids)
                    )
                )
                await session.execute(
                    delete(ScheduleAvailabilityWindowModel).where(
                        ScheduleAvailabilityWindowModel.candidate_set_id.in_(
                            candidate_ids
                        )
                    )
                )
            await session.execute(
                delete(ScheduleCandidateSetModel).where(
                    ScheduleCandidateSetModel.user_id == owner
                )
            )
            if snapshot_ids:
                await session.execute(
                    delete(ScheduleBusyIntervalModel).where(
                        ScheduleBusyIntervalModel.snapshot_id.in_(snapshot_ids)
                    )
                )
            await session.execute(
                delete(ScheduleBusySnapshotModel).where(
                    ScheduleBusySnapshotModel.user_id == owner
                )
            )
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id == owner)
            )
            await session.execute(
                delete(ContextSnapshotModel).where(
                    ContextSnapshotModel.user_id == owner
                )
            )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id == owner)
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_schedule_bundle_round_trip_isolation_and_locking(
    mysql_test_database: Database,
) -> None:
    now = datetime.now(UTC).replace(microsecond=123456)
    user_id, snapshot_id = uuid4(), uuid4()
    await _seed_owner(mysql_test_database, user_id, snapshot_id, now)
    repository = MySQLScheduleDraftRepository(mysql_test_database.session_factory)
    root_plan_id, session_id = uuid4(), uuid4()
    availability = (
        AvailabilityWindow(
            id=uuid4(),
            start=now + timedelta(days=1),
            end=now + timedelta(days=1, hours=2),
            location=LocationType.HOME,
        ),
    )
    busy = BusySnapshot(
        id=uuid4(),
        user_id=user_id,
        timezone="Asia/Shanghai",
        range_start_utc=now,
        range_end_utc=now + timedelta(days=7),
        mode=CalendarReadMode.MANUAL_ONLY,
        verification_status=CalendarVerificationStatus.MANUAL_ONLY,
        intervals=(
            BusyInterval(
                start=now + timedelta(hours=2),
                end=now + timedelta(hours=3),
                source=BusyIntervalSource.MANUAL,
            ),
        ),
        fingerprint="b" * 64,
        provider_summary="manual-only",
        provider_name="none",
        provider_version="v1",
        created_at=now,
    )
    slot = TimeSlotCandidate(
        id=uuid4(),
        session_id=session_id,
        slot_id="slot-one",
        start=availability[0].start,
        end=availability[0].start + timedelta(minutes=45),
        location=LocationType.HOME,
        preference_score=4,
        timezone="Asia/Shanghai",
        source_availability_id=availability[0].id or uuid4(),
    )
    candidate = TimeSlotCandidateSet(
        id=uuid4(),
        user_id=user_id,
        root_plan_id=root_plan_id,
        source_revision=1,
        busy_snapshot_id=busy.id,
        context_snapshot_reference_id=snapshot_id,
        slots=(slot,),
        session_candidates=(
            SessionSlotCandidates(session_id=session_id, slot_ids=(slot.slot_id,)),
        ),
        fingerprint="d" * 64,
        policy_version="schedule-candidate-set-v1",
        created_at=now,
    )
    draft = ScheduleDraft(
        id=uuid4(),
        request_id=uuid4(),
        client_request_id=f"schedule-{uuid4().hex}",
        user_id=user_id,
        request_payload_fingerprint="e" * 64,
        request_fingerprint="f" * 64,
        root_plan_id=root_plan_id,
        source_revision=1,
        source_plan_version=2,
        timezone="Asia/Shanghai",
        busy_snapshot_id=busy.id,
        candidate_set_id=candidate.id,
        candidate_set_fingerprint=candidate.fingerprint,
        context_snapshot_reference_id=snapshot_id,
        context_fingerprint="c" * 64,
        context_degraded_mode=ContextDegradedMode.NONE,
        assignments=(
            ScheduleAssignment(
                session_id=session_id,
                slot_id=slot.slot_id,
                scheduled_start=slot.start,
                scheduled_end=slot.end,
                location=slot.location,
            ),
        ),
        unresolved=(),
        outcome=ScheduleDraftOutcome.COMPLETE,
        source=ScheduleDraftSource.DETERMINISTIC_FALLBACK,
        prompt_version="schedule-agent-v1",
        provider_summary="gateway-disabled:deterministic-fallback",
        fallback_used=True,
        calendar_verification_status=CalendarVerificationStatus.MANUAL_ONLY,
        explanation_summary="Deterministic frozen slot selection.",
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    trace = ScheduleTrace(
        draft_id=draft.id,
        request_id=draft.request_id,
        busy_snapshot_id=busy.id,
        candidate_set_id=candidate.id,
        candidate_set_fingerprint=candidate.fingerprint,
        context_snapshot_reference_id=snapshot_id,
        context_fingerprint=draft.context_fingerprint,
        prompt_version=draft.prompt_version,
        provider_summary=draft.provider_summary,
        timezone=draft.timezone,
        provider_name="gateway-disabled",
        provider_version="v1",
        attempt_no=0,
        outcome="FALLBACK",
        validation_error_code=None,
        latency_ms=1.25,
        source=draft.source,
        fallback_used=True,
        provider_attempts=0,
        calendar_mode=CalendarReadMode.MANUAL_ONLY,
        calendar_attempts=0,
        model_trace_ids=(),
        created_at=now,
    )
    try:
        assert (
            await repository.save(draft, busy, candidate, trace, availability) == draft
        )
        rebuilt = MySQLScheduleDraftRepository(mysql_test_database.session_factory)
        assert await rebuilt.get_draft(user_id, draft.id) == draft
        assert await rebuilt.get_busy_snapshot(user_id, draft.id) == busy
        assert await rebuilt.get_candidate_set(user_id, draft.id) == candidate
        assert await rebuilt.get_trace(user_id, draft.id) == trace
        assert await rebuilt.get_availability(user_id, draft.id) == availability
        assert await rebuilt.get_draft(uuid4(), draft.id) is None
        accepted = draft.accept(now + timedelta(minutes=1))
        assert await rebuilt.update(accepted) == accepted
        with pytest.raises(RepositoryConflictError):
            await rebuilt.update(draft.reject(now + timedelta(minutes=2)))
    finally:
        await _cleanup(mysql_test_database, user_id)
