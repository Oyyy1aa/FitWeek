"""MySQL persistence for atomic Recovery Draft application facts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.recovery.models import RecoveryDraft
from app.domain.recovery_application.models import (
    RecoveryApplicationCommit,
    RecoveryApplicationResult,
    RecoveryMemoryProposalImportResult,
)
from app.domain.scheduling.models import ScheduleDraft
from app.domain.session_design.models import SessionDesignDraft
from app.persistence.mysql.models import (
    RecoveryActionCandidateModel,
    RecoveryApplicationResultModel,
    RecoveryDraftModel,
    RecoveryMemoryProposalImportModel,
    RecoveryScheduleSubdraftBindingModel,
    RecoverySessionDesignSubdraftBindingModel,
    ScheduleDraftModel,
    SessionDesignDraftModel,
    SessionExerciseModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)
from app.persistence.mysql.plan_repository import MySQLPlanRepository
from app.persistence.mysql.recovery_draft_repository import MySQLRecoveryDraftRepository
from app.persistence.mysql.schedule_application_repository import (
    MySQLScheduleApplicationRepository,
)
from app.persistence.mysql.session_design_application_repository import (
    MySQLSessionDesignApplicationRepository,
)


class MySQLRecoveryApplicationRepository:
    """Persist Recovery application commits with database CAS and unique winners."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_result(
        self, user_id: UUID, result_id: UUID
    ) -> RecoveryApplicationResult | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(RecoveryApplicationResultModel).where(
                    RecoveryApplicationResultModel.id == str(result_id),
                    RecoveryApplicationResultModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._result_from_row(row)

    async def get_result_by_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> RecoveryApplicationResult | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(RecoveryApplicationResultModel).where(
                    RecoveryApplicationResultModel.user_id == str(user_id),
                    RecoveryApplicationResultModel.recovery_draft_id == str(draft_id),
                )
            )
            return None if row is None else self._result_from_row(row)

    async def get_result_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> RecoveryApplicationResult | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(RecoveryApplicationResultModel).where(
                    RecoveryApplicationResultModel.user_id == str(user_id),
                    RecoveryApplicationResultModel.client_request_id
                    == client_request_id,
                )
            )
            return None if row is None else self._result_from_row(row)

    async def commit(
        self,
        *,
        source: WeeklyPlan,
        expected_draft: RecoveryDraft,
        applied_draft: RecoveryDraft,
        revision: WeeklyPlan | None,
        result: RecoveryApplicationResult,
        applied_session_design_drafts: tuple[SessionDesignDraft, ...],
        applied_schedule_drafts: tuple[ScheduleDraft, ...],
    ) -> RecoveryApplicationCommit:
        request_key = (result.user_id, result.client_request_id)
        async with self._sessions() as session:
            try:
                async with session.begin():
                    existing = await session.scalar(
                        select(RecoveryApplicationResultModel).where(
                            RecoveryApplicationResultModel.user_id
                            == str(result.user_id),
                            RecoveryApplicationResultModel.client_request_id
                            == result.client_request_id,
                        )
                    )
                    if existing is not None:
                        if not self._same_result(existing, result):
                            raise RepositoryUniqueError(
                                "recovery_application.user_request", request_key
                            )
                        return RecoveryApplicationCommit(
                            result=self._result_from_row(existing),
                            plan_revision_id=self._plan_id_for_result(
                                existing, revision
                            ),
                            created=False,
                        )

                    draft = await session.scalar(
                        select(RecoveryDraftModel)
                        .where(
                            RecoveryDraftModel.id == str(expected_draft.id),
                            RecoveryDraftModel.user_id == str(result.user_id),
                        )
                        .with_for_update()
                    )
                    existing_after_draft_lock = await session.scalar(
                        select(RecoveryApplicationResultModel)
                        .where(
                            RecoveryApplicationResultModel.user_id
                            == str(result.user_id),
                            RecoveryApplicationResultModel.client_request_id
                            == result.client_request_id,
                        )
                        .with_for_update()
                    )
                    if existing_after_draft_lock is not None:
                        if not self._same_result(existing_after_draft_lock, result):
                            raise RepositoryUniqueError(
                                "recovery_application.user_request", request_key
                            )
                        return RecoveryApplicationCommit(
                            result=self._result_from_row(existing_after_draft_lock),
                            plan_revision_id=self._plan_id_for_result(
                                existing_after_draft_lock, revision
                            ),
                            created=False,
                        )
                    if draft is None or (
                        draft.version != expected_draft.version
                        or draft.status != expected_draft.status.value
                    ):
                        raise RepositoryConflictError(
                            "RecoveryDraft",
                            expected_draft.id,
                            expected_version=expected_draft.version,
                            actual_version=0 if draft is None else draft.version,
                        )
                    if draft is not None and (
                        MySQLRecoveryDraftRepository._immutable_draft_fields(
                            MySQLRecoveryDraftRepository._draft_from_row(draft)
                        )
                        != MySQLRecoveryDraftRepository._immutable_draft_fields(
                            expected_draft
                        )
                    ):
                        raise RepositoryConflictError(
                            "RecoveryDraft",
                            expected_draft.id,
                            expected_version=expected_draft.version,
                            actual_version=draft.version,
                        )
                    if (
                        applied_draft.id != expected_draft.id
                        or applied_draft.user_id != result.user_id
                        or applied_draft.version != expected_draft.version + 1
                        or result.recovery_draft_id != expected_draft.id
                        or result.root_plan_id != expected_draft.root_plan_id
                        or result.source_revision != expected_draft.source_revision
                    ):
                        raise RepositoryConflictError(
                            "RecoveryDraft",
                            expected_draft.id,
                            expected_version=expected_draft.version + 1,
                            actual_version=applied_draft.version,
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
                            actual_version=0
                            if source_row is None
                            else source_row.version,
                        )
                    if (
                        source.id != expected_draft.root_plan_id
                        or source.revision != expected_draft.source_revision
                        or source.version != expected_draft.source_plan_version
                    ):
                        raise RepositoryConflictError(
                            "WeeklyPlan",
                            source.id,
                            expected_version=expected_draft.source_plan_version,
                            actual_version=source.version,
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
                    if revision is not None:
                        if (
                            revision.user_id != source.user_id
                            or revision.series_id != source.series_id
                            or revision.revision != source.revision + 1
                            or not revision.sessions
                        ):
                            raise RepositoryConflictError(
                                "WeeklyPlan.revision",
                                source.id,
                                expected_version=source.revision + 1,
                                actual_version=revision.revision,
                            )
                        duplicate = await session.scalar(
                            select(WeeklyPlanModel.id).where(
                                WeeklyPlanModel.user_id == str(revision.user_id),
                                WeeklyPlanModel.series_id == str(revision.series_id),
                                WeeklyPlanModel.revision == revision.revision,
                            )
                        )
                        if duplicate is not None:
                            raise RepositoryUniqueError(
                                "weekly_plan.user_week_revision", revision.id
                            )
                    duplicate_draft = await session.scalar(
                        select(RecoveryApplicationResultModel.id).where(
                            RecoveryApplicationResultModel.recovery_draft_id
                            == str(expected_draft.id)
                        )
                    )
                    if duplicate_draft is not None:
                        raise RepositoryUniqueError(
                            "recovery_application.draft", expected_draft.id
                        )

                    design_rows = cast(
                        list[SessionDesignDraftModel],
                        await self._locked_children(
                            session,
                            SessionDesignDraftModel,
                            applied_session_design_drafts,
                        ),
                    )
                    schedule_rows = cast(
                        list[ScheduleDraftModel],
                        await self._locked_children(
                            session, ScheduleDraftModel, applied_schedule_drafts
                        ),
                    )
                    if revision is not None:
                        session.add(MySQLPlanRepository._new_row(revision))
                        await session.flush()
                        await self._add_sessions(session, revision)
                    for design_row, design_value in zip(
                        design_rows, applied_session_design_drafts, strict=True
                    ):
                        MySQLSessionDesignApplicationRepository._apply_draft(
                            design_row, design_value
                        )
                    for schedule_row, schedule_value in zip(
                        schedule_rows, applied_schedule_drafts, strict=True
                    ):
                        MySQLScheduleApplicationRepository._apply_draft(
                            schedule_row, schedule_value
                        )
                    MySQLRecoveryDraftRepository._apply_draft(draft, applied_draft)
                    session.add(self._result_row(result))
                    await session.flush()
                stored = await self.get_result(result.user_id, result.id)
                if stored is None:
                    raise RuntimeError("Recovery application result was not persisted.")
                return RecoveryApplicationCommit(
                    result=stored,
                    plan_revision_id=None if revision is None else revision.id,
                    created=True,
                )
            except IntegrityError as exc:
                durable = await self.get_result_by_request(
                    result.user_id, result.client_request_id
                )
                if durable is not None and self._same_result_value(durable, result):
                    return RecoveryApplicationCommit(
                        result=durable,
                        plan_revision_id=self._plan_id_for_result_value(
                            durable, revision
                        ),
                        created=False,
                    )
                if durable is not None:
                    raise RepositoryUniqueError(
                        "recovery_application.user_request", request_key
                    ) from exc
                raise

    async def bind_session_design_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID, child_draft_id: UUID
    ) -> None:
        await self._bind(
            RecoverySessionDesignSubdraftBindingModel,
            SessionDesignDraftModel,
            "recovery.session_design_subdraft",
            recovery_draft_id,
            candidate_id,
            child_draft_id,
        )

    async def get_session_design_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID
    ) -> UUID | None:
        return await self._get_binding(
            RecoverySessionDesignSubdraftBindingModel, recovery_draft_id, candidate_id
        )

    async def bind_schedule_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID, child_draft_id: UUID
    ) -> None:
        await self._bind(
            RecoveryScheduleSubdraftBindingModel,
            ScheduleDraftModel,
            "recovery.schedule_subdraft",
            recovery_draft_id,
            candidate_id,
            child_draft_id,
        )

    async def get_schedule_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID
    ) -> UUID | None:
        return await self._get_binding(
            RecoveryScheduleSubdraftBindingModel, recovery_draft_id, candidate_id
        )

    async def get_memory_import(
        self, user_id: UUID, client_request_id: str
    ) -> RecoveryMemoryProposalImportResult | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(RecoveryMemoryProposalImportModel).where(
                    RecoveryMemoryProposalImportModel.user_id == str(user_id),
                    RecoveryMemoryProposalImportModel.client_request_id
                    == client_request_id,
                )
            )
            return None if row is None else self._import_from_row(row)

    async def save_memory_import(
        self, result: RecoveryMemoryProposalImportResult
    ) -> RecoveryMemoryProposalImportResult:
        key = (result.user_id, result.client_request_id)
        async with self._sessions() as session:
            try:
                async with session.begin():
                    existing_row = await session.scalar(
                        select(RecoveryMemoryProposalImportModel)
                        .where(
                            RecoveryMemoryProposalImportModel.user_id
                            == str(result.user_id),
                            RecoveryMemoryProposalImportModel.client_request_id
                            == result.client_request_id,
                        )
                        .with_for_update()
                    )
                    if existing_row is not None:
                        durable_row = self._import_from_row(existing_row)
                        if durable_row != result:
                            raise RepositoryUniqueError("recovery.memory_import", key)
                        return durable_row
                    draft = await session.scalar(
                        select(RecoveryDraftModel.id).where(
                            RecoveryDraftModel.id == str(result.draft_id),
                            RecoveryDraftModel.user_id == str(result.user_id),
                        )
                    )
                    if draft is None:
                        raise RepositoryConflictError(
                            "RecoveryDraft",
                            result.draft_id,
                            expected_version=1,
                            actual_version=0,
                        )
                    session.add(self._import_row(result))
                    await session.flush()
                stored_import = await self.get_memory_import(
                    result.user_id, result.client_request_id
                )
                if stored_import is None:
                    raise RuntimeError("Recovery memory import was not persisted.")
                return stored_import
            except IntegrityError as exc:
                durable = await self.get_memory_import(
                    result.user_id, result.client_request_id
                )
                if durable is not None and durable == result:
                    return durable
                if durable is not None:
                    raise RepositoryUniqueError("recovery.memory_import", key) from exc
                raise

    async def _bind(
        self,
        binding_type: Any,
        child_type: Any,
        unique_name: str,
        recovery_draft_id: UUID,
        candidate_id: UUID,
        child_draft_id: UUID,
    ) -> None:
        key = (recovery_draft_id, candidate_id)
        async with self._sessions() as session:
            try:
                async with session.begin():
                    draft = await session.scalar(
                        select(RecoveryDraftModel)
                        .where(RecoveryDraftModel.id == str(recovery_draft_id))
                        .with_for_update()
                    )
                    candidate = await session.scalar(
                        select(RecoveryActionCandidateModel).where(
                            RecoveryActionCandidateModel.id == str(candidate_id)
                        )
                    )
                    child = await session.scalar(
                        select(child_type).where(child_type.id == str(child_draft_id))
                    )
                    if (
                        draft is None
                        or candidate is None
                        or child is None
                        or draft.user_id != candidate.user_id
                        or draft.user_id != child.user_id
                    ):
                        raise RepositoryConflictError(
                            "RecoverySubdraftBinding",
                            recovery_draft_id,
                            expected_version=1,
                            actual_version=0,
                        )
                    existing = await session.scalar(
                        select(binding_type)
                        .where(
                            binding_type.recovery_draft_id == str(recovery_draft_id),
                            binding_type.candidate_id == str(candidate_id),
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        if existing.child_draft_id != str(child_draft_id):
                            raise RepositoryUniqueError(unique_name, key)
                        return
                    session.add(
                        binding_type(
                            recovery_draft_id=str(recovery_draft_id),
                            candidate_id=str(candidate_id),
                            child_draft_id=str(child_draft_id),
                            user_id=draft.user_id,
                            created_at=self._db_time(datetime.now(UTC)),
                        )
                    )
            except IntegrityError as exc:
                existing = await self._get_binding(
                    binding_type, recovery_draft_id, candidate_id
                )
                if existing == child_draft_id:
                    return
                if existing is not None:
                    raise RepositoryUniqueError(unique_name, key) from exc
                raise

    async def _get_binding(
        self,
        binding_type: Any,
        recovery_draft_id: UUID,
        candidate_id: UUID,
    ) -> UUID | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(binding_type.child_draft_id).where(
                    binding_type.recovery_draft_id == str(recovery_draft_id),
                    binding_type.candidate_id == str(candidate_id),
                )
            )
            return None if row is None else UUID(row)

    async def _locked_children(
        self,
        session: AsyncSession,
        model: Any,
        values: tuple[SessionDesignDraft, ...] | tuple[ScheduleDraft, ...],
    ) -> list[Any]:
        rows: list[Any] = []
        for value in values:
            row = await session.scalar(
                select(model)
                .where(model.id == str(value.id), model.user_id == str(value.user_id))
                .with_for_update()
            )
            if row is None or row.version != value.version - 1:
                raise RepositoryConflictError(
                    model.__name__.removesuffix("Model"),
                    value.id,
                    expected_version=value.version - 1,
                    actual_version=0 if row is None else row.version,
                )
            rows.append(row)
        return rows

    @classmethod
    async def _add_sessions(cls, session: AsyncSession, revision: WeeklyPlan) -> None:
        for item in revision.sessions:
            physical_id = uuid5(
                NAMESPACE_URL, f"fitweek:plan-revision-session:{revision.id}:{item.id}"
            )
            session.add(
                WorkoutSessionModel(
                    id=str(physical_id),
                    logical_session_id=str(item.id),
                    plan_id=str(revision.id),
                    scheduled_start=cls._db_time(item.scheduled_start),
                    scheduled_end=cls._db_time(item.scheduled_end),
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

    @staticmethod
    def _result_row(value: RecoveryApplicationResult) -> RecoveryApplicationResultModel:
        return RecoveryApplicationResultModel(
            id=str(value.id),
            user_id=str(value.user_id),
            client_request_id=value.client_request_id,
            application_fingerprint=value.application_fingerprint,
            request_fingerprint=value.request_fingerprint,
            recovery_draft_id=str(value.recovery_draft_id),
            root_plan_id=str(value.root_plan_id),
            source_revision=value.source_revision,
            created_revision=value.created_revision,
            applied_action_candidate_ids=[
                str(item) for item in value.applied_action_candidate_ids
            ],
            session_design_draft_ids=[
                str(item) for item in value.session_design_draft_ids
            ],
            schedule_draft_ids=[str(item) for item in value.schedule_draft_ids],
            affected_session_ids=[str(item) for item in value.affected_session_ids],
            removed_session_ids=[str(item) for item in value.removed_session_ids],
            preserved_session_ids=[str(item) for item in value.preserved_session_ids],
            immutable_session_ids=[str(item) for item in value.immutable_session_ids],
            outcome=value.outcome.value,
            created_at=MySQLRecoveryApplicationRepository._db_time(value.created_at),
        )

    @classmethod
    def _result_from_row(
        cls, row: RecoveryApplicationResultModel
    ) -> RecoveryApplicationResult:
        from app.domain.recovery_application.enums import RecoveryApplicationOutcome

        return RecoveryApplicationResult(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            client_request_id=row.client_request_id,
            application_fingerprint=row.application_fingerprint,
            request_fingerprint=row.request_fingerprint,
            recovery_draft_id=UUID(row.recovery_draft_id),
            root_plan_id=UUID(row.root_plan_id),
            source_revision=row.source_revision,
            created_revision=row.created_revision,
            applied_action_candidate_ids=tuple(
                UUID(item) for item in row.applied_action_candidate_ids
            ),
            session_design_draft_ids=tuple(
                UUID(item) for item in row.session_design_draft_ids
            ),
            schedule_draft_ids=tuple(UUID(item) for item in row.schedule_draft_ids),
            affected_session_ids=tuple(UUID(item) for item in row.affected_session_ids),
            removed_session_ids=tuple(UUID(item) for item in row.removed_session_ids),
            preserved_session_ids=tuple(
                UUID(item) for item in row.preserved_session_ids
            ),
            immutable_session_ids=tuple(
                UUID(item) for item in row.immutable_session_ids
            ),
            outcome=RecoveryApplicationOutcome(row.outcome),
            created_at=cls._utc(row.created_at),
        )

    @staticmethod
    def _import_row(
        value: RecoveryMemoryProposalImportResult,
    ) -> RecoveryMemoryProposalImportModel:
        return RecoveryMemoryProposalImportModel(
            id=str(value.id),
            user_id=str(value.user_id),
            draft_id=str(value.draft_id),
            client_request_id=value.client_request_id,
            fingerprint=value.fingerprint,
            proposal_ids=[str(item) for item in value.proposal_ids],
            memory_candidate_ids=[str(item) for item in value.memory_candidate_ids],
            created_at=MySQLRecoveryApplicationRepository._db_time(value.created_at),
        )

    @classmethod
    def _import_from_row(
        cls, row: RecoveryMemoryProposalImportModel
    ) -> RecoveryMemoryProposalImportResult:
        return RecoveryMemoryProposalImportResult(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            draft_id=UUID(row.draft_id),
            client_request_id=row.client_request_id,
            fingerprint=row.fingerprint,
            proposal_ids=tuple(UUID(item) for item in row.proposal_ids),
            memory_candidate_ids=tuple(UUID(item) for item in row.memory_candidate_ids),
            created_at=cls._utc(row.created_at),
        )

    @staticmethod
    def _same_result(
        row: RecoveryApplicationResultModel, value: RecoveryApplicationResult
    ) -> bool:
        return MySQLRecoveryApplicationRepository._result_from_row(row) == value

    @staticmethod
    def _same_result_value(
        existing: RecoveryApplicationResult, value: RecoveryApplicationResult
    ) -> bool:
        return existing == value

    @staticmethod
    def _plan_id_for_result(
        row: RecoveryApplicationResultModel, revision: WeeklyPlan | None
    ) -> UUID | None:
        return None if row.created_revision is None or revision is None else revision.id

    @staticmethod
    def _plan_id_for_result_value(
        existing: RecoveryApplicationResult, revision: WeeklyPlan | None
    ) -> UUID | None:
        return (
            None
            if existing.created_revision is None or revision is None
            else revision.id
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)
