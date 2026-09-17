"""Atomic MySQL commit for Schedule Draft applications."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.schedule_application.models import (
    ScheduleApplicationCommit,
    ScheduleApplicationResult,
)
from app.domain.scheduling.models import ScheduleDraft
from app.persistence.mysql.models import (
    AuditEventModel,
    ScheduleApplicationResultModel,
    ScheduleDraftModel,
    SessionExerciseModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)
from app.persistence.mysql.plan_repository import MySQLPlanRepository


class MySQLScheduleApplicationRepository:
    """Persist Draft transition, Plan Revision, sessions and result atomically."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_result(
        self, user_id: UUID, result_id: UUID
    ) -> ScheduleApplicationResult | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ScheduleApplicationResultModel).where(
                    ScheduleApplicationResultModel.id == str(result_id),
                    ScheduleApplicationResultModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._result_from_row(row)

    async def get_result_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> ScheduleApplicationResult | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ScheduleApplicationResultModel).where(
                    ScheduleApplicationResultModel.user_id == str(user_id),
                    ScheduleApplicationResultModel.client_request_id
                    == client_request_id,
                )
            )
            return None if row is None else self._result_from_row(row)

    async def get_result_by_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> ScheduleApplicationResult | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ScheduleApplicationResultModel).where(
                    ScheduleApplicationResultModel.user_id == str(user_id),
                    ScheduleApplicationResultModel.draft_id == str(draft_id),
                )
            )
            return None if row is None else self._result_from_row(row)

    async def commit(
        self,
        *,
        source: WeeklyPlan,
        expected_draft: ScheduleDraft,
        applied_draft: ScheduleDraft,
        revision: WeeklyPlan,
        result: ScheduleApplicationResult,
        _deadlock_retries: int = 1,
    ) -> ScheduleApplicationCommit:
        request_key = (result.user_id, result.client_request_id)
        async with self._sessions() as session:
            try:
                async with session.begin():
                    existing = await session.scalar(
                        select(ScheduleApplicationResultModel)
                        .where(
                            ScheduleApplicationResultModel.user_id
                            == str(result.user_id),
                            ScheduleApplicationResultModel.client_request_id
                            == result.client_request_id,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        if (
                            existing.application_fingerprint
                            != result.application_fingerprint
                            or existing.draft_id != str(result.schedule_draft_id)
                        ):
                            raise RepositoryUniqueError(
                                "schedule_application.user_request",
                                request_key,
                            )
                        return ScheduleApplicationCommit(
                            result=self._result_from_row(existing),
                            plan_revision_id=revision.id,
                            draft_id=result.schedule_draft_id,
                            created=False,
                        )

                    draft = await session.scalar(
                        select(ScheduleDraftModel)
                        .where(
                            ScheduleDraftModel.id == str(expected_draft.id),
                            ScheduleDraftModel.user_id == str(result.user_id),
                        )
                        .with_for_update()
                    )
                    if draft is None:
                        raise RepositoryConflictError(
                            "ScheduleDraft",
                            expected_draft.id,
                            expected_version=expected_draft.version,
                            actual_version=0,
                        )
                    if (
                        draft.version != expected_draft.version
                        or draft.status != expected_draft.status.value
                    ):
                        raise RepositoryConflictError(
                            "ScheduleDraft",
                            expected_draft.id,
                            expected_version=expected_draft.version,
                            actual_version=draft.version,
                        )

                    source_row = await session.scalar(
                        select(WeeklyPlanModel)
                        .where(
                            WeeklyPlanModel.id == str(source.id),
                            WeeklyPlanModel.user_id == str(source.user_id),
                        )
                        .with_for_update()
                    )
                    if source_row is None or source_row.version != source.version:
                        raise RepositoryConflictError(
                            "WeeklyPlan",
                            source.id,
                            expected_version=source.version,
                            actual_version=(
                                0 if source_row is None else source_row.version
                            ),
                        )
                    current = await session.scalar(
                        select(WeeklyPlanModel)
                        .where(
                            WeeklyPlanModel.user_id == str(source.user_id),
                            WeeklyPlanModel.series_id == str(source.series_id),
                            WeeklyPlanModel.status == WeeklyPlanStatus.CONFIRMED.value,
                        )
                        .order_by(WeeklyPlanModel.revision.desc())
                        .with_for_update()
                    )
                    if current is None or current.id != str(source.id):
                        raise RepositoryConflictError(
                            "WeeklyPlan.current_revision",
                            source.series_id,
                            expected_version=source.revision,
                            actual_version=0 if current is None else current.revision,
                        )
                    duplicate_revision = await session.scalar(
                        select(WeeklyPlanModel.id).where(
                            WeeklyPlanModel.user_id == str(revision.user_id),
                            WeeklyPlanModel.series_id == str(revision.series_id),
                            WeeklyPlanModel.revision == revision.revision,
                        )
                    )
                    if duplicate_revision is not None:
                        raise RepositoryUniqueError(
                            "weekly_plan.user_week_revision",
                            revision.id,
                        )
                    duplicate_draft = await session.scalar(
                        select(ScheduleApplicationResultModel.id).where(
                            ScheduleApplicationResultModel.draft_id
                            == str(expected_draft.id)
                        )
                    )
                    if duplicate_draft is not None:
                        raise RepositoryUniqueError(
                            "schedule_application.draft",
                            expected_draft.id,
                        )

                    session.add(MySQLPlanRepository._new_row(revision))
                    await session.flush()
                    for item in revision.sessions:
                        physical_id = uuid5(
                            NAMESPACE_URL,
                            f"fitweek:plan-revision-session:{revision.id}:{item.id}",
                        )
                        session.add(
                            WorkoutSessionModel(
                                id=str(physical_id),
                                logical_session_id=str(item.id),
                                plan_id=str(revision.id),
                                scheduled_start=self._db_time(item.scheduled_start),
                                scheduled_end=self._db_time(item.scheduled_end),
                                location_type=item.location_type.value,
                                session_type=item.session_type.value,
                                estimated_minutes=item.estimated_minutes,
                                target_difficulty=item.target_difficulty,
                                status=item.status.value,
                                schedule_source_metadata=[
                                    list(value)
                                    for value in item.schedule_source_metadata
                                ],
                                version=item.version,
                            )
                        )
                        await session.flush()
                        for exercise in item.exercises:
                            session.add(
                                SessionExerciseModel(
                                    session_id=str(physical_id),
                                    exercise_id=exercise.exercise_id,
                                    sequence_no=exercise.sequence_no,
                                    sets=exercise.sets,
                                    repetitions=exercise.repetitions,
                                    duration_seconds=exercise.duration_seconds,
                                    rest_seconds=exercise.rest_seconds,
                                )
                            )

                    self._apply_draft(draft, applied_draft)
                    session.add(self._result_row(result))
                    session.add(
                        AuditEventModel(
                            id=str(uuid4()),
                            user_id=str(result.user_id),
                            run_id=None,
                            step_id=None,
                            sequence_no=None,
                            event_type="SCHEDULE_DRAFT_APPLIED",
                            event_metadata={
                                "draft_id": str(result.schedule_draft_id),
                                "result_id": str(result.id),
                                "root_plan_id": str(result.root_plan_id),
                                "created_revision": str(result.created_revision),
                            },
                            occurred_at=self._db_time(result.created_at),
                        )
                    )
                return ScheduleApplicationCommit(
                    result=result,
                    plan_revision_id=revision.id,
                    draft_id=applied_draft.id,
                    created=True,
                )
            except IntegrityError as exc:
                by_request = await self.get_result_by_request(
                    result.user_id,
                    result.client_request_id,
                )
                if by_request is not None:
                    raise RepositoryUniqueError(
                        "schedule_application.user_request",
                        request_key,
                    ) from exc
                by_draft = await self.get_result_by_draft(
                    result.user_id,
                    result.schedule_draft_id,
                )
                if by_draft is not None:
                    raise RepositoryUniqueError(
                        "schedule_application.draft",
                        result.schedule_draft_id,
                    ) from exc
                raise RepositoryUniqueError(
                    "weekly_plan.user_week_revision",
                    revision.id,
                ) from exc
            except OperationalError as exc:
                if self._is_retryable_lock_error(exc) and _deadlock_retries > 0:
                    await asyncio.sleep(0)
                    return await self.commit(
                        source=source,
                        expected_draft=expected_draft,
                        applied_draft=applied_draft,
                        revision=revision,
                        result=result,
                        _deadlock_retries=_deadlock_retries - 1,
                    )
                raise RepositoryConflictError(
                    "ScheduleApplication",
                    result.id,
                    expected_version=expected_draft.version,
                    actual_version=expected_draft.version,
                ) from exc

    async def clear(self) -> None:
        async with self._sessions() as session:
            async with session.begin():
                if await session.scalar(text("DATABASE()")) != "fitweek_test":
                    raise RuntimeError(
                        "Schedule application reset is only permitted for fitweek_test"
                    )
                await session.execute(delete(ScheduleApplicationResultModel))

    @staticmethod
    def _is_retryable_lock_error(error: OperationalError) -> bool:
        arguments = getattr(error.orig, "args", ())
        return bool(arguments and arguments[0] in {1205, 1213})

    @staticmethod
    def _result_row(
        value: ScheduleApplicationResult,
    ) -> ScheduleApplicationResultModel:
        return ScheduleApplicationResultModel(
            id=str(value.id),
            user_id=str(value.user_id),
            client_request_id=value.client_request_id,
            application_fingerprint=value.application_fingerprint,
            draft_id=str(value.schedule_draft_id),
            root_plan_id=str(value.root_plan_id),
            source_revision=value.source_revision,
            created_revision=value.created_revision,
            previous_plan_version=value.previous_plan_version,
            resulting_plan_version=value.resulting_plan_version,
            changed_session_ids=[str(item) for item in value.changed_session_ids],
            calendar_verification_status=value.calendar_verification_status.value,
            created_at=MySQLScheduleApplicationRepository._db_time(value.created_at),
        )

    @classmethod
    def _result_from_row(
        cls, row: ScheduleApplicationResultModel
    ) -> ScheduleApplicationResult:
        from app.domain.scheduling.enums import CalendarVerificationStatus

        return ScheduleApplicationResult(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            client_request_id=row.client_request_id,
            application_fingerprint=row.application_fingerprint,
            schedule_draft_id=UUID(row.draft_id),
            root_plan_id=UUID(row.root_plan_id),
            source_revision=row.source_revision,
            created_revision=row.created_revision,
            previous_plan_version=row.previous_plan_version,
            resulting_plan_version=row.resulting_plan_version,
            changed_session_ids=tuple(UUID(item) for item in row.changed_session_ids),
            calendar_verification_status=CalendarVerificationStatus(
                row.calendar_verification_status
            ),
            created_at=cls._utc(row.created_at),
        )

    @classmethod
    def _apply_draft(cls, row: ScheduleDraftModel, value: ScheduleDraft) -> None:
        row.status = value.status.value
        row.reviewed_at = (
            None if value.reviewed_at is None else cls._db_time(value.reviewed_at)
        )
        row.applied_root_plan_id = (
            None
            if value.applied_root_plan_id is None
            else str(value.applied_root_plan_id)
        )
        row.applied_source_revision = value.applied_source_revision
        row.applied_created_revision = value.applied_created_revision
        row.application_result_id = (
            None
            if value.application_result_id is None
            else str(value.application_result_id)
        )
        row.applied_at = (
            None if value.applied_at is None else cls._db_time(value.applied_at)
        )
        row.updated_at = cls._db_time(
            value.applied_at or value.reviewed_at or value.created_at
        )
        row.version = value.version

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)
