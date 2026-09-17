"""MySQL persistence for frozen Schedule inputs and review Drafts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.common import (
    LocationType,
    RepositoryConflictError,
    RepositoryUniqueError,
)
from app.domain.context.enums import ContextDegradedMode
from app.domain.scheduling.enums import (
    BusyIntervalSource,
    CalendarReadMode,
    CalendarVerificationStatus,
    ScheduleDraftOutcome,
    ScheduleDraftSource,
    ScheduleDraftStatus,
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
    UnresolvedSession,
)
from app.persistence.mysql.models import (
    AuditEventModel,
    ScheduleApplicationResultModel,
    ScheduleAvailabilityWindowModel,
    ScheduleBusyIntervalModel,
    ScheduleBusySnapshotModel,
    ScheduleCandidateSetModel,
    ScheduleCandidateSlotModel,
    ScheduleDraftModel,
    ScheduleTraceModel,
)


class MySQLScheduleDraftRepository:
    """Implement the existing aggregate Schedule persistence port with MySQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_draft(self, user_id: UUID, draft_id: UUID) -> ScheduleDraft | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ScheduleDraftModel).where(
                    ScheduleDraftModel.id == str(draft_id),
                    ScheduleDraftModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._draft_from_row(row)

    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> ScheduleDraft | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ScheduleDraftModel).where(
                    ScheduleDraftModel.user_id == str(user_id),
                    ScheduleDraftModel.client_request_id == client_request_id,
                )
            )
            return None if row is None else self._draft_from_row(row)

    async def save(
        self,
        draft: ScheduleDraft,
        busy_snapshot: BusySnapshot,
        candidate_set: TimeSlotCandidateSet,
        trace: ScheduleTrace,
        availability_windows: tuple[AvailabilityWindow, ...] = (),
    ) -> ScheduleDraft:
        async with self._sessions() as session:
            try:
                async with session.begin():
                    existing_busy = await session.get(
                        ScheduleBusySnapshotModel, str(busy_snapshot.id)
                    )
                    if existing_busy is None:
                        session.add(self._busy_row(busy_snapshot))
                        await session.flush()
                        for order, interval in enumerate(
                            busy_snapshot.intervals, start=1
                        ):
                            session.add(
                                ScheduleBusyIntervalModel(
                                    snapshot_id=str(busy_snapshot.id),
                                    user_id=str(busy_snapshot.user_id),
                                    starts_at_utc=self._db_time(interval.start),
                                    ends_at_utc=self._db_time(interval.end),
                                    source=interval.source.value,
                                    interval_order=order,
                                )
                            )
                    elif (
                        await self._busy_from_row(session, existing_busy)
                        != busy_snapshot
                    ):
                        raise RepositoryUniqueError(
                            "schedule_busy_snapshot.id", busy_snapshot.id
                        )

                    existing_candidate = await session.get(
                        ScheduleCandidateSetModel, str(candidate_set.id)
                    )
                    if existing_candidate is None:
                        session.add(self._candidate_row(candidate_set))
                        await session.flush()
                        for order, window in enumerate(availability_windows, start=1):
                            assert window.id is not None
                            session.add(
                                ScheduleAvailabilityWindowModel(
                                    id=str(window.id),
                                    candidate_set_id=str(candidate_set.id),
                                    user_id=str(candidate_set.user_id),
                                    starts_at_utc=self._db_time(window.start),
                                    ends_at_utc=self._db_time(window.end),
                                    location=window.location.value,
                                    window_order=order,
                                )
                            )
                        for order, slot in enumerate(candidate_set.slots, start=1):
                            session.add(
                                ScheduleCandidateSlotModel(
                                    id=str(slot.id),
                                    candidate_set_id=str(candidate_set.id),
                                    user_id=str(candidate_set.user_id),
                                    slot_id=slot.slot_id,
                                    session_id=str(slot.session_id),
                                    starts_at_utc=self._db_time(slot.start),
                                    ends_at_utc=self._db_time(slot.end),
                                    location=slot.location.value,
                                    preference_score=slot.preference_score,
                                    timezone=slot.timezone,
                                    source_availability_id=str(
                                        slot.source_availability_id
                                    ),
                                    candidate_order=order,
                                )
                            )
                    elif (
                        await self._candidate_from_row(session, existing_candidate)
                        != candidate_set
                    ):
                        raise RepositoryUniqueError(
                            "schedule_candidate_set.id", candidate_set.id
                        )
                    session.add(self._draft_row(draft))
                    session.add(self._trace_row(trace))
                    self._audit(
                        session,
                        draft.user_id,
                        "SCHEDULE_DRAFT_CREATED",
                        {
                            "draft_id": str(draft.id),
                            "candidate_set_id": str(candidate_set.id),
                            "busy_snapshot_id": str(busy_snapshot.id),
                        },
                        draft.created_at,
                    )
                return draft
            except IntegrityError as exc:
                raise RepositoryUniqueError(
                    "schedule_draft.user_request",
                    (draft.user_id, draft.client_request_id),
                ) from exc

    async def update(self, draft: ScheduleDraft) -> ScheduleDraft:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(ScheduleDraftModel)
                    .where(
                        ScheduleDraftModel.id == str(draft.id),
                        ScheduleDraftModel.user_id == str(draft.user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    raise RepositoryUniqueError("schedule_draft.id", draft.id)
                expected = row.version + 1
                if draft.version != expected:
                    raise RepositoryConflictError(
                        "ScheduleDraft",
                        draft.id,
                        expected_version=expected,
                        actual_version=draft.version,
                    )
                self._apply_draft(row, draft)
                self._audit(
                    session,
                    draft.user_id,
                    f"SCHEDULE_DRAFT_{draft.status.value}",
                    {"draft_id": str(draft.id)},
                    draft.reviewed_at or draft.applied_at or draft.created_at,
                )
            return draft

    async def get_busy_snapshot(
        self, user_id: UUID, draft_id: UUID
    ) -> BusySnapshot | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ScheduleBusySnapshotModel)
                .join(
                    ScheduleDraftModel,
                    ScheduleDraftModel.busy_snapshot_id == ScheduleBusySnapshotModel.id,
                )
                .where(
                    ScheduleDraftModel.id == str(draft_id),
                    ScheduleDraftModel.user_id == str(user_id),
                    ScheduleBusySnapshotModel.user_id == str(user_id),
                )
            )
            return None if row is None else await self._busy_from_row(session, row)

    async def get_candidate_set(
        self, user_id: UUID, draft_id: UUID
    ) -> TimeSlotCandidateSet | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ScheduleCandidateSetModel)
                .join(
                    ScheduleDraftModel,
                    ScheduleDraftModel.candidate_set_id == ScheduleCandidateSetModel.id,
                )
                .where(
                    ScheduleDraftModel.id == str(draft_id),
                    ScheduleDraftModel.user_id == str(user_id),
                    ScheduleCandidateSetModel.user_id == str(user_id),
                )
            )
            return None if row is None else await self._candidate_from_row(session, row)

    async def get_trace(self, user_id: UUID, draft_id: UUID) -> ScheduleTrace | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ScheduleTraceModel)
                .join(
                    ScheduleDraftModel,
                    ScheduleDraftModel.id == ScheduleTraceModel.draft_id,
                )
                .where(
                    ScheduleTraceModel.draft_id == str(draft_id),
                    ScheduleDraftModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._trace_from_row(row)

    async def get_availability(
        self, user_id: UUID, draft_id: UUID
    ) -> tuple[AvailabilityWindow, ...]:
        """Return the immutable normalized Availability used for this Draft."""

        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(ScheduleAvailabilityWindowModel)
                    .join(
                        ScheduleDraftModel,
                        ScheduleDraftModel.candidate_set_id
                        == ScheduleAvailabilityWindowModel.candidate_set_id,
                    )
                    .where(
                        ScheduleDraftModel.id == str(draft_id),
                        ScheduleDraftModel.user_id == str(user_id),
                        ScheduleAvailabilityWindowModel.user_id == str(user_id),
                    )
                    .order_by(ScheduleAvailabilityWindowModel.window_order)
                )
            ).all()
            return tuple(
                AvailabilityWindow(
                    id=UUID(row.id),
                    start=self._utc(row.starts_at_utc),
                    end=self._utc(row.ends_at_utc),
                    location=LocationType(row.location),
                )
                for row in rows
            )

    async def clear(self) -> None:
        """Clear only the isolated integration database."""

        async with self._sessions() as session:
            async with session.begin():
                if await session.scalar(text("DATABASE()")) != "fitweek_test":
                    raise RuntimeError(
                        "Schedule reset is only permitted for fitweek_test"
                    )
                await session.execute(delete(ScheduleApplicationResultModel))
                await session.execute(delete(ScheduleTraceModel))
                await session.execute(delete(ScheduleDraftModel))
                await session.execute(delete(ScheduleCandidateSlotModel))
                await session.execute(delete(ScheduleAvailabilityWindowModel))
                await session.execute(delete(ScheduleCandidateSetModel))
                await session.execute(delete(ScheduleBusyIntervalModel))
                await session.execute(delete(ScheduleBusySnapshotModel))

    @staticmethod
    def _busy_row(value: BusySnapshot) -> ScheduleBusySnapshotModel:
        return ScheduleBusySnapshotModel(
            id=str(value.id),
            user_id=str(value.user_id),
            timezone=value.timezone,
            range_start_utc=MySQLScheduleDraftRepository._db_time(
                value.range_start_utc
            ),
            range_end_utc=MySQLScheduleDraftRepository._db_time(value.range_end_utc),
            mode=value.mode.value,
            verification_status=value.verification_status.value,
            fingerprint=value.fingerprint,
            provider_summary=value.provider_summary,
            provider_name=value.provider_name,
            provider_version=value.provider_version,
            created_at=MySQLScheduleDraftRepository._db_time(value.created_at),
        )

    @classmethod
    async def _busy_from_row(
        cls, session: AsyncSession, row: ScheduleBusySnapshotModel
    ) -> BusySnapshot:
        intervals = (
            await session.scalars(
                select(ScheduleBusyIntervalModel)
                .where(ScheduleBusyIntervalModel.snapshot_id == row.id)
                .order_by(ScheduleBusyIntervalModel.interval_order)
            )
        ).all()
        return BusySnapshot(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            timezone=row.timezone,
            range_start_utc=cls._utc(row.range_start_utc),
            range_end_utc=cls._utc(row.range_end_utc),
            mode=CalendarReadMode(row.mode),
            verification_status=CalendarVerificationStatus(row.verification_status),
            intervals=tuple(
                BusyInterval(
                    start=cls._utc(item.starts_at_utc),
                    end=cls._utc(item.ends_at_utc),
                    source=BusyIntervalSource(item.source),
                )
                for item in intervals
            ),
            fingerprint=row.fingerprint,
            provider_summary=row.provider_summary,
            provider_name=row.provider_name,
            provider_version=row.provider_version,
            created_at=cls._utc(row.created_at),
        )

    @staticmethod
    def _candidate_row(value: TimeSlotCandidateSet) -> ScheduleCandidateSetModel:
        return ScheduleCandidateSetModel(
            id=str(value.id),
            user_id=str(value.user_id),
            root_plan_id=str(value.root_plan_id),
            source_revision=value.source_revision,
            busy_snapshot_id=str(value.busy_snapshot_id),
            context_snapshot_id=str(value.context_snapshot_reference_id),
            session_candidates=[
                {
                    "session_id": str(item.session_id),
                    "slot_ids": list(item.slot_ids),
                }
                for item in value.session_candidates
            ],
            fingerprint=value.fingerprint,
            policy_version=value.policy_version,
            created_at=MySQLScheduleDraftRepository._db_time(value.created_at),
        )

    @classmethod
    async def _candidate_from_row(
        cls, session: AsyncSession, row: ScheduleCandidateSetModel
    ) -> TimeSlotCandidateSet:
        slots = (
            await session.scalars(
                select(ScheduleCandidateSlotModel)
                .where(ScheduleCandidateSlotModel.candidate_set_id == row.id)
                .order_by(ScheduleCandidateSlotModel.candidate_order)
            )
        ).all()
        mappings = row.session_candidates
        return TimeSlotCandidateSet(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            root_plan_id=UUID(row.root_plan_id),
            source_revision=row.source_revision,
            busy_snapshot_id=UUID(row.busy_snapshot_id),
            context_snapshot_reference_id=UUID(row.context_snapshot_id),
            slots=tuple(
                TimeSlotCandidate(
                    id=UUID(item.id),
                    session_id=UUID(item.session_id),
                    slot_id=item.slot_id,
                    start=cls._utc(item.starts_at_utc),
                    end=cls._utc(item.ends_at_utc),
                    location=LocationType(item.location),
                    preference_score=item.preference_score,
                    timezone=item.timezone,
                    source_availability_id=UUID(item.source_availability_id),
                )
                for item in slots
            ),
            session_candidates=tuple(
                SessionSlotCandidates(
                    session_id=UUID(str(item["session_id"])),
                    slot_ids=tuple(str(slot) for slot in item["slot_ids"]),
                )
                for item in mappings
            ),
            fingerprint=row.fingerprint,
            policy_version=row.policy_version,
            created_at=cls._utc(row.created_at),
        )

    @classmethod
    def _draft_row(cls, value: ScheduleDraft) -> ScheduleDraftModel:
        updated = value.applied_at or value.reviewed_at or value.created_at
        return ScheduleDraftModel(
            id=str(value.id),
            request_id=str(value.request_id),
            client_request_id=value.client_request_id,
            user_id=str(value.user_id),
            request_payload_fingerprint=value.request_payload_fingerprint,
            request_fingerprint=value.request_fingerprint,
            root_plan_id=str(value.root_plan_id),
            source_revision=value.source_revision,
            source_plan_version=value.source_plan_version,
            timezone=value.timezone,
            busy_snapshot_id=str(value.busy_snapshot_id),
            candidate_set_id=str(value.candidate_set_id),
            candidate_set_fingerprint=value.candidate_set_fingerprint,
            context_snapshot_id=str(value.context_snapshot_reference_id),
            context_fingerprint=value.context_fingerprint,
            context_degraded_mode=value.context_degraded_mode.value,
            assignments=[
                {
                    "session_id": str(item.session_id),
                    "slot_id": item.slot_id,
                    "scheduled_start": item.scheduled_start.isoformat(),
                    "scheduled_end": item.scheduled_end.isoformat(),
                    "location": item.location.value,
                }
                for item in value.assignments
            ],
            unresolved=[
                {
                    "session_id": str(item.session_id),
                    "code": item.code,
                    "message": item.message,
                }
                for item in value.unresolved
            ],
            outcome=value.outcome.value,
            source=value.source.value,
            prompt_version=value.prompt_version,
            provider_summary=value.provider_summary,
            fallback_used=value.fallback_used,
            calendar_verification_status=value.calendar_verification_status.value,
            explanation_summary=value.explanation_summary,
            status=value.status.value,
            reviewed_at=(
                None if value.reviewed_at is None else cls._db_time(value.reviewed_at)
            ),
            applied_root_plan_id=(
                None
                if value.applied_root_plan_id is None
                else str(value.applied_root_plan_id)
            ),
            applied_source_revision=value.applied_source_revision,
            applied_created_revision=value.applied_created_revision,
            application_result_id=(
                None
                if value.application_result_id is None
                else str(value.application_result_id)
            ),
            applied_at=(
                None if value.applied_at is None else cls._db_time(value.applied_at)
            ),
            created_at=cls._db_time(value.created_at),
            expires_at=cls._db_time(value.expires_at),
            updated_at=cls._db_time(updated),
            version=value.version,
        )

    @classmethod
    def _draft_from_row(cls, row: ScheduleDraftModel) -> ScheduleDraft:
        return ScheduleDraft(
            id=UUID(row.id),
            request_id=UUID(row.request_id),
            client_request_id=row.client_request_id,
            user_id=UUID(row.user_id),
            request_payload_fingerprint=row.request_payload_fingerprint,
            request_fingerprint=row.request_fingerprint,
            root_plan_id=UUID(row.root_plan_id),
            source_revision=row.source_revision,
            source_plan_version=row.source_plan_version,
            timezone=row.timezone,
            busy_snapshot_id=UUID(row.busy_snapshot_id),
            candidate_set_id=UUID(row.candidate_set_id),
            candidate_set_fingerprint=row.candidate_set_fingerprint,
            context_snapshot_reference_id=UUID(row.context_snapshot_id),
            context_fingerprint=row.context_fingerprint,
            context_degraded_mode=ContextDegradedMode(row.context_degraded_mode),
            assignments=tuple(
                ScheduleAssignment(
                    session_id=UUID(str(item["session_id"])),
                    slot_id=str(item["slot_id"]),
                    scheduled_start=datetime.fromisoformat(
                        str(item["scheduled_start"])
                    ).astimezone(UTC),
                    scheduled_end=datetime.fromisoformat(
                        str(item["scheduled_end"])
                    ).astimezone(UTC),
                    location=LocationType(str(item["location"])),
                )
                for item in row.assignments
            ),
            unresolved=tuple(
                UnresolvedSession(
                    session_id=UUID(str(item["session_id"])),
                    code=str(item["code"]),
                    message=str(item["message"]),
                )
                for item in row.unresolved
            ),
            outcome=ScheduleDraftOutcome(row.outcome),
            source=ScheduleDraftSource(row.source),
            prompt_version=row.prompt_version,
            provider_summary=row.provider_summary,
            fallback_used=row.fallback_used,
            calendar_verification_status=CalendarVerificationStatus(
                row.calendar_verification_status
            ),
            explanation_summary=row.explanation_summary,
            created_at=cls._utc(row.created_at),
            expires_at=cls._utc(row.expires_at),
            status=ScheduleDraftStatus(row.status),
            version=row.version,
            reviewed_at=(
                None if row.reviewed_at is None else cls._utc(row.reviewed_at)
            ),
            applied_root_plan_id=(
                None
                if row.applied_root_plan_id is None
                else UUID(row.applied_root_plan_id)
            ),
            applied_source_revision=row.applied_source_revision,
            applied_created_revision=row.applied_created_revision,
            application_result_id=(
                None
                if row.application_result_id is None
                else UUID(row.application_result_id)
            ),
            applied_at=(None if row.applied_at is None else cls._utc(row.applied_at)),
        )

    @classmethod
    def _apply_draft(cls, row: ScheduleDraftModel, value: ScheduleDraft) -> None:
        updated = cls._draft_row(value)
        for field in (
            "status",
            "reviewed_at",
            "applied_root_plan_id",
            "applied_source_revision",
            "applied_created_revision",
            "application_result_id",
            "applied_at",
            "updated_at",
            "version",
        ):
            setattr(row, field, getattr(updated, field))

    @staticmethod
    def _trace_row(value: ScheduleTrace) -> ScheduleTraceModel:
        return ScheduleTraceModel(
            draft_id=str(value.draft_id),
            request_id=str(value.request_id),
            busy_snapshot_id=str(value.busy_snapshot_id),
            candidate_set_id=str(value.candidate_set_id),
            candidate_set_fingerprint=value.candidate_set_fingerprint,
            context_snapshot_id=str(value.context_snapshot_reference_id),
            context_fingerprint=value.context_fingerprint,
            prompt_version=value.prompt_version,
            provider_summary=value.provider_summary,
            timezone=value.timezone,
            provider_name=value.provider_name,
            provider_version=value.provider_version,
            attempt_no=value.attempt_no,
            outcome=value.outcome,
            validation_error_code=value.validation_error_code,
            latency_ms=Decimal(str(value.latency_ms)),
            source=value.source.value,
            fallback_used=value.fallback_used,
            provider_attempts=value.provider_attempts,
            calendar_mode=value.calendar_mode.value,
            calendar_attempts=value.calendar_attempts,
            model_trace_ids=[str(item) for item in value.model_trace_ids],
            created_at=MySQLScheduleDraftRepository._db_time(value.created_at),
        )

    @classmethod
    def _trace_from_row(cls, row: ScheduleTraceModel) -> ScheduleTrace:
        return ScheduleTrace(
            draft_id=UUID(row.draft_id),
            request_id=UUID(row.request_id),
            busy_snapshot_id=UUID(row.busy_snapshot_id),
            candidate_set_id=UUID(row.candidate_set_id),
            candidate_set_fingerprint=row.candidate_set_fingerprint,
            context_snapshot_reference_id=UUID(row.context_snapshot_id),
            context_fingerprint=row.context_fingerprint,
            prompt_version=row.prompt_version,
            provider_summary=row.provider_summary,
            timezone=row.timezone,
            provider_name=row.provider_name,
            provider_version=row.provider_version,
            attempt_no=row.attempt_no,
            outcome=row.outcome,
            validation_error_code=row.validation_error_code,
            latency_ms=float(row.latency_ms),
            source=ScheduleDraftSource(row.source),
            fallback_used=row.fallback_used,
            provider_attempts=row.provider_attempts,
            calendar_mode=CalendarReadMode(row.calendar_mode),
            calendar_attempts=row.calendar_attempts,
            model_trace_ids=tuple(UUID(item) for item in row.model_trace_ids),
            created_at=cls._utc(row.created_at),
        )

    @staticmethod
    def _audit(
        session: AsyncSession,
        user_id: UUID,
        event_type: str,
        metadata: dict[str, str],
        at: datetime,
    ) -> None:
        session.add(
            AuditEventModel(
                id=str(uuid4()),
                user_id=str(user_id),
                run_id=None,
                step_id=None,
                sequence_no=None,
                event_type=event_type,
                event_metadata=metadata,
                occurred_at=MySQLScheduleDraftRepository._db_time(at),
            )
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)
