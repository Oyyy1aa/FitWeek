"""MySQL persistence for weekly plans, revisions, and revision-scoped sessions."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.common import (
    LocationType,
    RepositoryConflictError,
    RepositoryUniqueError,
    utc_now,
)
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.recovery_application.models import RecoveryPlanApplicationMetadata
from app.domain.replanning.models import PlanChangeMetadata, PlanChangeType
from app.domain.schedule_application.models import ScheduleApplicationMetadata
from app.domain.session_design_application.models import (
    SessionDesignApplicationMetadata,
)
from app.domain.sessions.models import (
    SessionExercise,
    SessionType,
    WorkoutSession,
    WorkoutSessionStatus,
)
from app.persistence.mysql.models import (
    IdempotencyRecordModel,
    SessionExerciseModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)


class MySQLPlanRepository:
    """Store each plan revision with its own immutable session snapshot."""

    _GENERATION = "plan.generation"

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, plan_id: UUID) -> WeeklyPlan | None:
        async with self._sessions() as session:
            row = await session.get(WeeklyPlanModel, str(plan_id))
            return None if row is None else await self._to_plan(session, row)

    async def get_for_user(self, plan_id: UUID, user_id: UUID) -> WeeklyPlan | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(WeeklyPlanModel).where(
                    WeeklyPlanModel.id == str(plan_id),
                    WeeklyPlanModel.user_id == str(user_id),
                )
            )
            return None if row is None else await self._to_plan(session, row)

    async def save(self, plan: WeeklyPlan) -> WeeklyPlan:
        async with self._sessions() as session:
            try:
                async with session.begin():
                    row = await session.get(WeeklyPlanModel, str(plan.id))
                    if row is None:
                        if plan.version != 1:
                            raise RepositoryConflictError(
                                "weekly_plan",
                                plan.id,
                                expected_version=1,
                                actual_version=plan.version,
                            )
                        existing = await session.scalar(
                            select(WeeklyPlanModel.id).where(
                                WeeklyPlanModel.user_id == str(plan.user_id),
                                WeeklyPlanModel.series_id == str(plan.series_id),
                                WeeklyPlanModel.revision == plan.revision,
                            )
                        )
                        if existing is not None:
                            raise RepositoryUniqueError("weekly_plan.revision", plan.id)
                        session.add(self._new_row(plan))
                        await session.flush()
                        for item in plan.sessions:
                            session.add(self._new_session(plan.id, item))
                            await session.flush()
                            for exercise in item.exercises:
                                session.add(self._new_exercise(item.id, exercise))
                    else:
                        if row.version + 1 != plan.version or row.user_id != str(
                            plan.user_id
                        ):
                            raise RepositoryConflictError(
                                "weekly_plan",
                                plan.id,
                                expected_version=row.version + 1,
                                actual_version=plan.version,
                            )
                        row.status = plan.status.value
                        row.goal_snapshot = plan.goal_snapshot
                        row.constraint_snapshot = list(plan.constraint_snapshot)
                        row.estimated_total_minutes = plan.estimated_total_minutes
                        row.generation_metadata = plan.generation_metadata
                        row.updated_at = MySQLPlanRepository._db_time(plan.updated_at)
                        row.confirmed_at = (
                            None
                            if plan.confirmed_at is None
                            else MySQLPlanRepository._db_time(plan.confirmed_at)
                        )
                        row.version = plan.version
                return plan
            except IntegrityError as exc:
                raise RepositoryUniqueError("weekly_plan.constraint", plan.id) from exc

    async def list_by_user(self, user_id: UUID) -> list[WeeklyPlan]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(WeeklyPlanModel)
                    .where(WeeklyPlanModel.user_id == str(user_id))
                    .order_by(
                        WeeklyPlanModel.week_start,
                        WeeklyPlanModel.revision,
                        WeeklyPlanModel.id,
                    )
                )
            ).all()
            return [await self._to_plan(session, row) for row in rows]

    async def list_sessions(self, plan_id: UUID) -> list[WorkoutSession]:
        async with self._sessions() as session:
            return await self._list_sessions(session, plan_id)

    async def list_sessions_for_user(
        self, plan_id: UUID, user_id: UUID
    ) -> list[WorkoutSession]:
        if await self.get_for_user(plan_id, user_id) is None:
            return []
        return await self.list_sessions(plan_id)

    async def get_session(self, session_id: UUID) -> WorkoutSession | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(WorkoutSessionModel)
                .join(WeeklyPlanModel)
                .where(WorkoutSessionModel.logical_session_id == str(session_id))
                .order_by(WeeklyPlanModel.revision.desc())
            )
            return None if row is None else await self._to_session(session, row)

    async def get_session_for_user(
        self, session_id: UUID, user_id: UUID
    ) -> WorkoutSession | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(WorkoutSessionModel)
                .join(WeeklyPlanModel)
                .where(
                    WorkoutSessionModel.logical_session_id == str(session_id),
                    WeeklyPlanModel.user_id == str(user_id),
                )
                .order_by(WeeklyPlanModel.revision.desc())
            )
            return None if row is None else await self._to_session(session, row)

    async def get_current_plan_for_session(
        self, session_id: UUID, user_id: UUID
    ) -> WeeklyPlan | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(WeeklyPlanModel)
                .join(WorkoutSessionModel)
                .where(
                    WorkoutSessionModel.logical_session_id == str(session_id),
                    WeeklyPlanModel.user_id == str(user_id),
                )
                .order_by(WeeklyPlanModel.revision.desc())
            )
            return None if row is None else await self._to_plan(session, row)

    async def list_revisions_for_user(
        self, series_id: UUID, user_id: UUID
    ) -> list[WeeklyPlan]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(WeeklyPlanModel)
                    .where(
                        WeeklyPlanModel.series_id == str(series_id),
                        WeeklyPlanModel.user_id == str(user_id),
                    )
                    .order_by(WeeklyPlanModel.revision)
                )
            ).all()
            return [await self._to_plan(session, row) for row in rows]

    async def get_revision_for_user(
        self, series_id: UUID, user_id: UUID, revision: int
    ) -> WeeklyPlan | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(WeeklyPlanModel).where(
                    WeeklyPlanModel.series_id == str(series_id),
                    WeeklyPlanModel.user_id == str(user_id),
                    WeeklyPlanModel.revision == revision,
                )
            )
            return None if row is None else await self._to_plan(session, row)

    async def get_current_confirmed(
        self, series_id: UUID, user_id: UUID
    ) -> WeeklyPlan | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(WeeklyPlanModel)
                .where(
                    WeeklyPlanModel.series_id == str(series_id),
                    WeeklyPlanModel.user_id == str(user_id),
                    WeeklyPlanModel.status == WeeklyPlanStatus.CONFIRMED.value,
                )
                .order_by(WeeklyPlanModel.revision.desc())
            )
            return None if row is None else await self._to_plan(session, row)

    async def find_by_replan_request(
        self, user_id: UUID, client_request_id: str
    ) -> WeeklyPlan | None:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(WeeklyPlanModel).where(
                        WeeklyPlanModel.user_id == str(user_id),
                        WeeklyPlanModel.change_metadata.is_not(None),
                    )
                )
            ).all()
            for row in rows:
                metadata = self._change_metadata_from_payload(row.change_metadata)
                if (
                    isinstance(metadata, PlanChangeMetadata)
                    and metadata.client_request_id == client_request_id
                ):
                    return await self._to_plan(session, row)
            return None

    async def get_generation_request(
        self, user_id: UUID, client_request_id: str
    ) -> tuple[str, WeeklyPlan] | None:
        async with self._sessions() as session:
            record = await session.scalar(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.user_id == str(user_id),
                    IdempotencyRecordModel.operation == self._GENERATION,
                    IdempotencyRecordModel.request_key == client_request_id,
                )
            )
            if record is None or record.resource_id is None:
                return None
            plan = await session.get(WeeklyPlanModel, record.resource_id)
            return (
                None
                if plan is None
                else (record.payload_fingerprint, await self._to_plan(session, plan))
            )

    async def bind_generation_request(
        self,
        user_id: UUID,
        client_request_id: str,
        input_fingerprint: str,
        plan_id: UUID,
    ) -> None:
        async with self._sessions() as session:
            try:
                async with session.begin():
                    row = await session.scalar(
                        select(IdempotencyRecordModel).where(
                            IdempotencyRecordModel.user_id == str(user_id),
                            IdempotencyRecordModel.operation == self._GENERATION,
                            IdempotencyRecordModel.request_key == client_request_id,
                        )
                    )
                    if row is None:
                        session.add(
                            IdempotencyRecordModel(
                                id=str(uuid4()),
                                user_id=str(user_id),
                                operation=self._GENERATION,
                                request_key=client_request_id,
                                payload_fingerprint=input_fingerprint,
                                resource_id=str(plan_id),
                                created_at=utc_now(),
                            )
                        )
                    elif (
                        row.payload_fingerprint != input_fingerprint
                        or row.resource_id != str(plan_id)
                    ):
                        raise RepositoryUniqueError(
                            "plan.generation", client_request_id
                        )
            except IntegrityError as exc:
                raise RepositoryUniqueError(
                    "plan.generation", client_request_id
                ) from exc

    @classmethod
    async def _to_plan(cls, session: AsyncSession, row: WeeklyPlanModel) -> WeeklyPlan:
        return WeeklyPlan(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            week_start=row.week_start,
            status=WeeklyPlanStatus(row.status),
            revision=row.revision,
            goal_snapshot=row.goal_snapshot,
            constraint_snapshot=tuple(row.constraint_snapshot),
            estimated_total_minutes=row.estimated_total_minutes,
            sessions=tuple(await cls._list_sessions(session, UUID(row.id))),
            created_at=cls._utc(row.created_at),
            updated_at=cls._utc(row.updated_at),
            confirmed_at=None
            if row.confirmed_at is None
            else cls._utc(row.confirmed_at),
            version=row.version,
            generation_metadata=row.generation_metadata,
            root_plan_id=(None if row.id == row.series_id else UUID(row.series_id)),
            parent_revision=row.parent_revision,
            revision_reason=row.revision_reason,
            change_metadata=cls._change_metadata_from_payload(row.change_metadata),
        )

    @classmethod
    async def _list_sessions(
        cls, session: AsyncSession, plan_id: UUID
    ) -> list[WorkoutSession]:
        rows = (
            await session.scalars(
                select(WorkoutSessionModel)
                .where(WorkoutSessionModel.plan_id == str(plan_id))
                .order_by(WorkoutSessionModel.scheduled_start, WorkoutSessionModel.id)
            )
        ).all()
        return [await cls._to_session(session, row) for row in rows]

    @classmethod
    async def _to_session(
        cls, session: AsyncSession, row: WorkoutSessionModel
    ) -> WorkoutSession:
        plan = await session.get(WeeklyPlanModel, row.plan_id)
        if plan is None:
            raise RepositoryConflictError(
                "workout_session.plan",
                UUID(row.id),
                expected_version=1,
                actual_version=0,
            )
        exercises = (
            await session.scalars(
                select(SessionExerciseModel)
                .where(SessionExerciseModel.session_id == row.id)
                .order_by(SessionExerciseModel.sequence_no)
            )
        ).all()
        return WorkoutSession(
            id=UUID(row.logical_session_id),
            plan_id=UUID(plan.series_id),
            scheduled_start=cls._utc(row.scheduled_start),
            scheduled_end=cls._utc(row.scheduled_end),
            location_type=LocationType(row.location_type),
            session_type=SessionType(row.session_type),
            estimated_minutes=row.estimated_minutes,
            target_difficulty=row.target_difficulty,
            status=WorkoutSessionStatus(row.status),
            exercises=tuple(
                SessionExercise(
                    exercise_id=item.exercise_id,
                    sequence_no=item.sequence_no,
                    sets=item.sets,
                    repetitions=item.repetitions,
                    duration_seconds=item.duration_seconds,
                    rest_seconds=item.rest_seconds,
                )
                for item in exercises
            ),
            version=row.version,
            schedule_source_metadata=tuple(
                (item[0], item[1]) for item in row.schedule_source_metadata
            ),
        )

    @staticmethod
    def _new_row(plan: WeeklyPlan) -> WeeklyPlanModel:
        return WeeklyPlanModel(
            id=str(plan.id),
            user_id=str(plan.user_id),
            series_id=str(plan.series_id),
            week_start=plan.week_start,
            status=plan.status.value,
            revision=plan.revision,
            goal_snapshot=plan.goal_snapshot,
            constraint_snapshot=list(plan.constraint_snapshot),
            estimated_total_minutes=plan.estimated_total_minutes,
            generation_metadata=plan.generation_metadata,
            change_metadata=MySQLPlanRepository._change_metadata_to_payload(
                plan.change_metadata
            ),
            parent_revision=plan.parent_revision,
            revision_reason=plan.revision_reason,
            created_at=MySQLPlanRepository._db_time(plan.created_at),
            updated_at=MySQLPlanRepository._db_time(plan.updated_at),
            confirmed_at=(
                None
                if plan.confirmed_at is None
                else MySQLPlanRepository._db_time(plan.confirmed_at)
            ),
            version=plan.version,
        )

    @staticmethod
    def _new_session(plan_id: UUID, item: WorkoutSession) -> WorkoutSessionModel:
        return WorkoutSessionModel(
            id=str(item.id),
            logical_session_id=str(item.id),
            plan_id=str(plan_id),
            scheduled_start=MySQLPlanRepository._db_time(item.scheduled_start),
            scheduled_end=MySQLPlanRepository._db_time(item.scheduled_end),
            location_type=item.location_type.value,
            session_type=item.session_type.value,
            estimated_minutes=item.estimated_minutes,
            target_difficulty=item.target_difficulty,
            status=item.status.value,
            schedule_source_metadata=[
                list(value) for value in item.schedule_source_metadata
            ],
            version=item.version,
        )

    @staticmethod
    def _new_exercise(session_id: UUID, item: SessionExercise) -> SessionExerciseModel:
        return SessionExerciseModel(
            session_id=str(session_id),
            exercise_id=item.exercise_id,
            sequence_no=item.sequence_no,
            sets=item.sets,
            repetitions=item.repetitions,
            duration_seconds=item.duration_seconds,
            rest_seconds=item.rest_seconds,
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)

    @staticmethod
    def _change_metadata_to_payload(
        value: object,
    ) -> dict[str, object] | None:
        if value is None:
            return None
        if isinstance(value, SessionDesignApplicationMetadata):
            return {
                "kind": "session_design_application",
                "source_draft_id": str(value.source_draft_id),
                "source_draft_version": value.source_draft_version,
                "source_candidate_set_id": str(value.source_candidate_set_id),
                "source_context_snapshot_reference_id": str(
                    value.source_context_snapshot_reference_id
                ),
                "source_revision": value.source_revision,
                "target_session_id": str(value.target_session_id),
                "changed_session_ids": [
                    str(item) for item in value.changed_session_ids
                ],
                "preserved_session_ids": [
                    str(item) for item in value.preserved_session_ids
                ],
                "immutable_session_ids": [
                    str(item) for item in value.immutable_session_ids
                ],
                "application_fingerprint": value.application_fingerprint,
                "policy_version": value.policy_version,
            }
        if isinstance(value, ScheduleApplicationMetadata):
            return {
                "kind": "schedule_application",
                "source_schedule_draft_id": str(value.source_schedule_draft_id),
                "source_schedule_draft_version": (value.source_schedule_draft_version),
                "source_context_snapshot_reference_id": str(
                    value.source_context_snapshot_reference_id
                ),
                "source_busy_snapshot_id": str(value.source_busy_snapshot_id),
                "source_candidate_set_id": str(value.source_candidate_set_id),
                "source_revision": value.source_revision,
                "created_revision": value.created_revision,
                "changed_session_ids": [
                    str(item) for item in value.changed_session_ids
                ],
                "preserved_session_ids": [
                    str(item) for item in value.preserved_session_ids
                ],
                "immutable_session_ids": [
                    str(item) for item in value.immutable_session_ids
                ],
                "calendar_verification_status": (
                    value.calendar_verification_status.value
                ),
                "application_fingerprint": value.application_fingerprint,
                "policy_version": value.policy_version,
            }
        if isinstance(value, RecoveryPlanApplicationMetadata):
            return {
                "kind": "recovery_application",
                "source_recovery_draft_id": str(value.source_recovery_draft_id),
                "source_recovery_draft_version": value.source_recovery_draft_version,
                "source_candidate_set_id": str(value.source_candidate_set_id),
                "source_behavior_summary_id": str(value.source_behavior_summary_id),
                "source_change_impact_snapshot_id": str(
                    value.source_change_impact_snapshot_id
                ),
                "source_revision": value.source_revision,
                "created_revision": value.created_revision,
                "applied_action_candidate_ids": [
                    str(item) for item in value.applied_action_candidate_ids
                ],
                "session_design_draft_ids": [
                    str(item) for item in value.session_design_draft_ids
                ],
                "schedule_draft_ids": [str(item) for item in value.schedule_draft_ids],
                "changed_session_ids": [
                    str(item) for item in value.changed_session_ids
                ],
                "removed_session_ids": [
                    str(item) for item in value.removed_session_ids
                ],
                "preserved_session_ids": [
                    str(item) for item in value.preserved_session_ids
                ],
                "immutable_session_ids": [
                    str(item) for item in value.immutable_session_ids
                ],
                "application_fingerprint": value.application_fingerprint,
                "policy_version": value.policy_version,
            }
        if not isinstance(value, PlanChangeMetadata):
            raise RepositoryConflictError(
                "weekly_plan.change_metadata",
                UUID(int=0),
                expected_version=1,
                actual_version=0,
            )
        return {
            "kind": "local_replan",
            "change_type": value.change_type.value,
            "source_revision": value.source_revision,
            "changed_session_ids": [str(item) for item in value.changed_session_ids],
            "preserved_session_ids": [
                str(item) for item in value.preserved_session_ids
            ],
            "immutable_session_ids": [
                str(item) for item in value.immutable_session_ids
            ],
            "change_fingerprint": value.change_fingerprint,
            "replanning_policy_version": value.replanning_policy_version,
            "client_request_id": value.client_request_id,
        }

    @staticmethod
    def _change_metadata_from_payload(
        value: dict[str, object] | None,
    ) -> (
        PlanChangeMetadata
        | SessionDesignApplicationMetadata
        | ScheduleApplicationMetadata
        | RecoveryPlanApplicationMetadata
        | None
    ):
        if value is None:
            return None
        if value.get("kind") == "session_design_application":
            try:
                changed = cast(list[object], value["changed_session_ids"])
                preserved = cast(list[object], value["preserved_session_ids"])
                immutable = cast(list[object], value["immutable_session_ids"])
                return SessionDesignApplicationMetadata(
                    source_draft_id=UUID(str(value["source_draft_id"])),
                    source_draft_version=int(
                        cast(str | int, value["source_draft_version"])
                    ),
                    source_candidate_set_id=UUID(str(value["source_candidate_set_id"])),
                    source_context_snapshot_reference_id=UUID(
                        str(value["source_context_snapshot_reference_id"])
                    ),
                    source_revision=int(cast(str | int, value["source_revision"])),
                    target_session_id=UUID(str(value["target_session_id"])),
                    changed_session_ids=tuple(UUID(str(item)) for item in changed),
                    preserved_session_ids=tuple(UUID(str(item)) for item in preserved),
                    immutable_session_ids=tuple(UUID(str(item)) for item in immutable),
                    application_fingerprint=str(value["application_fingerprint"]),
                    policy_version=str(value["policy_version"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RepositoryConflictError(
                    "weekly_plan.change_metadata",
                    UUID(int=0),
                    expected_version=1,
                    actual_version=0,
                ) from exc
        if value.get("kind") == "schedule_application":
            try:
                from app.domain.scheduling.enums import (
                    CalendarVerificationStatus,
                )

                changed = cast(list[object], value["changed_session_ids"])
                preserved = cast(list[object], value["preserved_session_ids"])
                immutable = cast(list[object], value["immutable_session_ids"])
                return ScheduleApplicationMetadata(
                    source_schedule_draft_id=UUID(
                        str(value["source_schedule_draft_id"])
                    ),
                    source_schedule_draft_version=int(
                        cast(str | int, value["source_schedule_draft_version"])
                    ),
                    source_context_snapshot_reference_id=UUID(
                        str(value["source_context_snapshot_reference_id"])
                    ),
                    source_busy_snapshot_id=UUID(str(value["source_busy_snapshot_id"])),
                    source_candidate_set_id=UUID(str(value["source_candidate_set_id"])),
                    source_revision=int(cast(str | int, value["source_revision"])),
                    created_revision=int(cast(str | int, value["created_revision"])),
                    changed_session_ids=tuple(UUID(str(item)) for item in changed),
                    preserved_session_ids=tuple(UUID(str(item)) for item in preserved),
                    immutable_session_ids=tuple(UUID(str(item)) for item in immutable),
                    calendar_verification_status=CalendarVerificationStatus(
                        str(value["calendar_verification_status"])
                    ),
                    application_fingerprint=str(value["application_fingerprint"]),
                    policy_version=str(value["policy_version"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RepositoryConflictError(
                    "weekly_plan.change_metadata",
                    UUID(int=0),
                    expected_version=1,
                    actual_version=0,
                ) from exc
        if value.get("kind") == "recovery_application":
            try:
                action_ids = cast(list[object], value["applied_action_candidate_ids"])
                design_ids = cast(list[object], value["session_design_draft_ids"])
                schedule_ids = cast(list[object], value["schedule_draft_ids"])
                changed = cast(list[object], value["changed_session_ids"])
                removed = cast(list[object], value["removed_session_ids"])
                preserved = cast(list[object], value["preserved_session_ids"])
                immutable = cast(list[object], value["immutable_session_ids"])
                return RecoveryPlanApplicationMetadata(
                    source_recovery_draft_id=UUID(
                        str(value["source_recovery_draft_id"])
                    ),
                    source_recovery_draft_version=int(
                        cast(str | int, value["source_recovery_draft_version"])
                    ),
                    source_candidate_set_id=UUID(str(value["source_candidate_set_id"])),
                    source_behavior_summary_id=UUID(
                        str(value["source_behavior_summary_id"])
                    ),
                    source_change_impact_snapshot_id=UUID(
                        str(value["source_change_impact_snapshot_id"])
                    ),
                    source_revision=int(cast(str | int, value["source_revision"])),
                    created_revision=int(cast(str | int, value["created_revision"])),
                    applied_action_candidate_ids=tuple(
                        UUID(str(item)) for item in action_ids
                    ),
                    session_design_draft_ids=tuple(
                        UUID(str(item)) for item in design_ids
                    ),
                    schedule_draft_ids=tuple(UUID(str(item)) for item in schedule_ids),
                    changed_session_ids=tuple(UUID(str(item)) for item in changed),
                    removed_session_ids=tuple(UUID(str(item)) for item in removed),
                    preserved_session_ids=tuple(UUID(str(item)) for item in preserved),
                    immutable_session_ids=tuple(UUID(str(item)) for item in immutable),
                    application_fingerprint=str(value["application_fingerprint"]),
                    policy_version=str(value["policy_version"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RepositoryConflictError(
                    "weekly_plan.change_metadata",
                    UUID(int=0),
                    expected_version=1,
                    actual_version=0,
                ) from exc
        if value.get("kind") != "local_replan":
            return None
        try:
            changed = cast(list[object], value["changed_session_ids"])
            preserved = cast(list[object], value["preserved_session_ids"])
            immutable = cast(list[object], value["immutable_session_ids"])
            return PlanChangeMetadata(
                change_type=PlanChangeType(str(value["change_type"])),
                source_revision=int(cast(str | int, value["source_revision"])),
                changed_session_ids=tuple(UUID(str(item)) for item in changed),
                preserved_session_ids=tuple(UUID(str(item)) for item in preserved),
                immutable_session_ids=tuple(UUID(str(item)) for item in immutable),
                change_fingerprint=str(value["change_fingerprint"]),
                replanning_policy_version=str(value["replanning_policy_version"]),
                client_request_id=str(value["client_request_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RepositoryConflictError(
                "weekly_plan.change_metadata",
                UUID(int=0),
                expected_version=1,
                actual_version=0,
            ) from exc
