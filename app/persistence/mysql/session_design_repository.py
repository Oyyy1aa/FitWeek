"""MySQL persistence for frozen Session Design inputs and review Drafts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
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
from app.domain.profiles.models import FitnessGoal
from app.domain.session_design.enums import (
    SessionDesignDraftStatus,
    SessionDesignSource,
    SessionExerciseRole,
    SessionTemplateId,
)
from app.domain.session_design.models import (
    CandidateSlot,
    ExerciseCandidateSet,
    SessionDesignDraft,
    SessionDesignTrace,
    SessionDurationBreakdown,
)
from app.domain.sessions.models import SessionExercise, SessionType
from app.persistence.mysql.models import (
    AuditEventModel,
    SessionDesignCandidateSetModel,
    SessionDesignCandidateSlotModel,
    SessionDesignDraftModel,
    SessionDesignTraceModel,
)
from app.safety.models import SafetyValidationResult, SafetyViolation


class MySQLSessionDesignRepository:
    """Implement the existing aggregate persistence port with MySQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> SessionDesignDraft | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(SessionDesignDraftModel).where(
                    SessionDesignDraftModel.id == str(draft_id),
                    SessionDesignDraftModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._draft_from_row(row)

    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> SessionDesignDraft | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(SessionDesignDraftModel).where(
                    SessionDesignDraftModel.user_id == str(user_id),
                    SessionDesignDraftModel.client_request_id == client_request_id,
                )
            )
            return None if row is None else self._draft_from_row(row)

    async def save_candidate_set(
        self, candidate_set: ExerciseCandidateSet
    ) -> ExerciseCandidateSet:
        async with self._sessions() as session:
            try:
                async with session.begin():
                    current = await session.get(
                        SessionDesignCandidateSetModel,
                        str(candidate_set.id),
                    )
                    if current is not None:
                        restored = await self._candidate_from_row(session, current)
                        if restored != candidate_set:
                            raise RepositoryUniqueError(
                                "session_candidate_set.id",
                                candidate_set.id,
                            )
                        return restored
                    existing = await session.scalar(
                        select(SessionDesignCandidateSetModel).where(
                            SessionDesignCandidateSetModel.user_id
                            == str(candidate_set.user_id),
                            SessionDesignCandidateSetModel.request_fingerprint
                            == candidate_set.request_fingerprint,
                            SessionDesignCandidateSetModel.fingerprint
                            == candidate_set.fingerprint,
                        )
                    )
                    if existing is not None:
                        return await self._candidate_from_row(session, existing)
                    session.add(self._candidate_row(candidate_set))
                    await session.flush()
                    for order, slot in enumerate(candidate_set.slots, start=1):
                        session.add(
                            SessionDesignCandidateSlotModel(
                                candidate_set_id=str(candidate_set.id),
                                slot_id=slot.slot_id,
                                role=slot.role.value,
                                exercise_ids=list(slot.exercise_ids),
                                candidate_order=order,
                            )
                        )
                return candidate_set
            except IntegrityError as exc:
                async with self._sessions() as retry:
                    existing = await retry.scalar(
                        select(SessionDesignCandidateSetModel).where(
                            SessionDesignCandidateSetModel.user_id
                            == str(candidate_set.user_id),
                            SessionDesignCandidateSetModel.request_fingerprint
                            == candidate_set.request_fingerprint,
                            SessionDesignCandidateSetModel.fingerprint
                            == candidate_set.fingerprint,
                        )
                    )
                    if existing is not None:
                        return await self._candidate_from_row(retry, existing)
                raise RepositoryUniqueError(
                    "session_candidate_set.fingerprint",
                    candidate_set.id,
                ) from exc

    async def get_candidate_set(
        self, user_id: UUID, candidate_set_id: UUID
    ) -> ExerciseCandidateSet | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(SessionDesignCandidateSetModel).where(
                    SessionDesignCandidateSetModel.id == str(candidate_set_id),
                    SessionDesignCandidateSetModel.user_id == str(user_id),
                )
            )
            return None if row is None else await self._candidate_from_row(session, row)

    async def save_draft(
        self, draft: SessionDesignDraft, trace: SessionDesignTrace
    ) -> SessionDesignDraft:
        async with self._sessions() as session:
            try:
                async with session.begin():
                    session.add(self._draft_row(draft))
                    session.add(self._trace_row(trace))
                    self._audit(
                        session,
                        draft.user_id,
                        "SESSION_DESIGN_DRAFT_CREATED",
                        {"draft_id": str(draft.id)},
                        draft.created_at,
                    )
                return draft
            except IntegrityError as exc:
                raise RepositoryUniqueError(
                    "session_design.request",
                    (draft.user_id, draft.client_request_id),
                ) from exc

    async def update_draft(self, draft: SessionDesignDraft) -> SessionDesignDraft:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(SessionDesignDraftModel)
                    .where(
                        SessionDesignDraftModel.id == str(draft.id),
                        SessionDesignDraftModel.user_id == str(draft.user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    raise RepositoryUniqueError("session_design.id", draft.id)
                expected = row.version + 1
                if draft.version != expected:
                    raise RepositoryConflictError(
                        "SessionDesignDraft",
                        draft.id,
                        expected_version=expected,
                        actual_version=draft.version,
                    )
                self._apply_draft(row, draft)
                self._audit(
                    session,
                    draft.user_id,
                    f"SESSION_DESIGN_DRAFT_{draft.status.value}",
                    {"draft_id": str(draft.id)},
                    draft.reviewed_at or draft.applied_at or draft.created_at,
                )
            return draft

    async def get_trace(
        self, user_id: UUID, draft_id: UUID
    ) -> SessionDesignTrace | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(SessionDesignTraceModel)
                .join(
                    SessionDesignDraftModel,
                    SessionDesignDraftModel.id == SessionDesignTraceModel.draft_id,
                )
                .where(
                    SessionDesignTraceModel.draft_id == str(draft_id),
                    SessionDesignDraftModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._trace_from_row(row)

    async def clear(self) -> None:
        """Clear only the isolated integration database."""

        async with self._sessions() as session:
            async with session.begin():
                if await session.scalar(text("DATABASE()")) != "fitweek_test":
                    raise RuntimeError(
                        "Session Design reset is only permitted for fitweek_test"
                    )
                await session.execute(delete(SessionDesignTraceModel))
                await session.execute(delete(SessionDesignDraftModel))
                await session.execute(delete(SessionDesignCandidateSlotModel))
                await session.execute(delete(SessionDesignCandidateSetModel))

    @staticmethod
    def _candidate_row(
        value: ExerciseCandidateSet,
    ) -> SessionDesignCandidateSetModel:
        return SessionDesignCandidateSetModel(
            id=str(value.id),
            user_id=str(value.user_id),
            request_fingerprint=value.request_fingerprint,
            fingerprint=value.fingerprint,
            template_id=value.template_id.value,
            template_version=value.template_version,
            catalog_version=value.catalog_version,
            context_snapshot_id=str(value.context_snapshot_reference_id),
            context_fingerprint=value.context_fingerprint,
            created_at=MySQLSessionDesignRepository._db_time(value.created_at),
        )

    @classmethod
    async def _candidate_from_row(
        cls,
        session: AsyncSession,
        row: SessionDesignCandidateSetModel,
    ) -> ExerciseCandidateSet:
        slots = (
            await session.scalars(
                select(SessionDesignCandidateSlotModel)
                .where(SessionDesignCandidateSlotModel.candidate_set_id == row.id)
                .order_by(SessionDesignCandidateSlotModel.candidate_order)
            )
        ).all()
        return ExerciseCandidateSet(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            request_fingerprint=row.request_fingerprint,
            fingerprint=row.fingerprint,
            template_id=SessionTemplateId(row.template_id),
            template_version=row.template_version,
            catalog_version=row.catalog_version,
            context_snapshot_reference_id=UUID(row.context_snapshot_id),
            context_fingerprint=row.context_fingerprint,
            slots=tuple(
                CandidateSlot(
                    slot_id=item.slot_id,
                    role=SessionExerciseRole(item.role),
                    exercise_ids=tuple(item.exercise_ids),
                )
                for item in slots
            ),
            created_at=cls._utc(row.created_at),
        )

    @classmethod
    def _draft_row(cls, value: SessionDesignDraft) -> SessionDesignDraftModel:
        return SessionDesignDraftModel(
            id=str(value.id),
            request_id=str(value.request_id),
            client_request_id=value.client_request_id,
            user_id=str(value.user_id),
            request_payload_fingerprint=value.request_payload_fingerprint,
            candidate_set_id=str(value.candidate_set_id),
            candidate_set_fingerprint=value.candidate_set_fingerprint,
            context_snapshot_id=str(value.context_snapshot_reference_id),
            context_fingerprint=value.context_fingerprint,
            context_degraded_mode=value.context_degraded_mode.value,
            template_id=value.template_id.value,
            template_version=value.template_version,
            catalog_version=value.catalog_version,
            session_type=value.session_type.value,
            target_date=value.target_date,
            target_duration_minutes=value.target_duration_minutes,
            location=value.location.value,
            goal=value.goal.value,
            exercises=[cls._exercise_payload(item) for item in value.exercises],
            exercise_roles=[item.value for item in value.exercise_roles],
            duration={
                "exercise_seconds": value.duration.exercise_seconds,
                "rest_seconds": value.duration.rest_seconds,
                "transition_seconds": value.duration.transition_seconds,
                "total_seconds": value.duration.total_seconds,
                "policy_version": value.duration.policy_version,
            },
            safety_validation=cls._safety_payload(value.safety_validation),
            source=value.source.value,
            prompt_version=value.prompt_version,
            provider_summary=value.provider_summary,
            fallback_used=value.fallback_used,
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
            applied_revision=value.applied_revision,
            applied_session_id=(
                None
                if value.applied_session_id is None
                else str(value.applied_session_id)
            ),
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
            updated_at=cls._db_time(
                value.applied_at or value.reviewed_at or value.created_at
            ),
            version=value.version,
        )

    @classmethod
    def _draft_from_row(cls, row: SessionDesignDraftModel) -> SessionDesignDraft:
        duration = row.duration
        return SessionDesignDraft(
            id=UUID(row.id),
            request_id=UUID(row.request_id),
            client_request_id=row.client_request_id,
            user_id=UUID(row.user_id),
            request_payload_fingerprint=row.request_payload_fingerprint,
            candidate_set_id=UUID(row.candidate_set_id),
            candidate_set_fingerprint=row.candidate_set_fingerprint,
            context_snapshot_reference_id=UUID(row.context_snapshot_id),
            context_fingerprint=row.context_fingerprint,
            context_degraded_mode=ContextDegradedMode(row.context_degraded_mode),
            template_id=SessionTemplateId(row.template_id),
            template_version=row.template_version,
            catalog_version=row.catalog_version,
            session_type=SessionType(row.session_type),
            target_date=row.target_date,
            target_duration_minutes=row.target_duration_minutes,
            location=LocationType(row.location),
            goal=FitnessGoal(row.goal),
            exercises=tuple(cls._exercise_from_payload(item) for item in row.exercises),
            exercise_roles=tuple(
                SessionExerciseRole(item) for item in row.exercise_roles
            ),
            duration=SessionDurationBreakdown(
                exercise_seconds=int(duration["exercise_seconds"]),
                rest_seconds=int(duration["rest_seconds"]),
                transition_seconds=int(duration["transition_seconds"]),
                total_seconds=int(duration["total_seconds"]),
                policy_version=str(duration["policy_version"]),
            ),
            safety_validation=cls._safety_from_payload(row.safety_validation),
            source=SessionDesignSource(row.source),
            prompt_version=row.prompt_version,
            provider_summary=row.provider_summary,
            fallback_used=row.fallback_used,
            explanation_summary=row.explanation_summary,
            created_at=cls._utc(row.created_at),
            expires_at=cls._utc(row.expires_at),
            status=SessionDesignDraftStatus(row.status),
            version=row.version,
            reviewed_at=(
                None if row.reviewed_at is None else cls._utc(row.reviewed_at)
            ),
            applied_root_plan_id=(
                None
                if row.applied_root_plan_id is None
                else UUID(row.applied_root_plan_id)
            ),
            applied_revision=row.applied_revision,
            applied_session_id=(
                None if row.applied_session_id is None else UUID(row.applied_session_id)
            ),
            application_result_id=(
                None
                if row.application_result_id is None
                else UUID(row.application_result_id)
            ),
            applied_at=None if row.applied_at is None else cls._utc(row.applied_at),
        )

    @classmethod
    def _apply_draft(
        cls, row: SessionDesignDraftModel, value: SessionDesignDraft
    ) -> None:
        updated = cls._draft_row(value)
        for field in (
            "status",
            "reviewed_at",
            "applied_root_plan_id",
            "applied_revision",
            "applied_session_id",
            "application_result_id",
            "applied_at",
            "updated_at",
            "version",
        ):
            setattr(row, field, getattr(updated, field))

    @staticmethod
    def _trace_row(value: SessionDesignTrace) -> SessionDesignTraceModel:
        return SessionDesignTraceModel(
            draft_id=str(value.draft_id),
            request_id=str(value.request_id),
            candidate_set_id=str(value.candidate_set_id),
            candidate_set_fingerprint=value.candidate_set_fingerprint,
            context_snapshot_id=str(value.context_snapshot_reference_id),
            context_fingerprint=value.context_fingerprint,
            prompt_version=value.prompt_version,
            template_id=value.template_id.value,
            template_version=value.template_version,
            provider_summary=value.provider_summary,
            source=value.source.value,
            fallback_used=value.fallback_used,
            validation_error_code=value.validation_error_code,
            model_trace_ids=[str(item) for item in value.model_trace_ids],
        )

    @staticmethod
    def _trace_from_row(row: SessionDesignTraceModel) -> SessionDesignTrace:
        return SessionDesignTrace(
            draft_id=UUID(row.draft_id),
            request_id=UUID(row.request_id),
            candidate_set_id=UUID(row.candidate_set_id),
            candidate_set_fingerprint=row.candidate_set_fingerprint,
            context_snapshot_reference_id=UUID(row.context_snapshot_id),
            context_fingerprint=row.context_fingerprint,
            prompt_version=row.prompt_version,
            template_id=SessionTemplateId(row.template_id),
            template_version=row.template_version,
            provider_summary=row.provider_summary,
            source=SessionDesignSource(row.source),
            fallback_used=row.fallback_used,
            validation_error_code=row.validation_error_code,
            model_trace_ids=tuple(UUID(item) for item in row.model_trace_ids),
        )

    @staticmethod
    def _exercise_payload(value: SessionExercise) -> dict[str, Any]:
        return {
            "exercise_id": value.exercise_id,
            "sequence_no": value.sequence_no,
            "sets": value.sets,
            "repetitions": value.repetitions,
            "duration_seconds": value.duration_seconds,
            "rest_seconds": value.rest_seconds,
        }

    @staticmethod
    def _exercise_from_payload(value: dict[str, Any]) -> SessionExercise:
        return SessionExercise(
            exercise_id=str(value["exercise_id"]),
            sequence_no=int(value["sequence_no"]),
            sets=None if value["sets"] is None else int(value["sets"]),
            repetitions=(
                None if value["repetitions"] is None else int(value["repetitions"])
            ),
            duration_seconds=(
                None
                if value["duration_seconds"] is None
                else int(value["duration_seconds"])
            ),
            rest_seconds=int(value["rest_seconds"]),
        )

    @staticmethod
    def _safety_payload(value: SafetyValidationResult) -> dict[str, Any]:
        return {
            "passed": value.passed,
            "violations": [
                {
                    "code": item.code,
                    "message": item.message,
                    "path": item.path,
                    "session_id": (
                        None if item.session_id is None else str(item.session_id)
                    ),
                    "exercise_id": item.exercise_id,
                }
                for item in value.violations
            ],
        }

    @staticmethod
    def _safety_from_payload(payload: dict[str, Any]) -> SafetyValidationResult:
        return SafetyValidationResult.from_violations(
            tuple(
                SafetyViolation(
                    code=str(item["code"]),
                    message=str(item["message"]),
                    path=None if item["path"] is None else str(item["path"]),
                    session_id=(
                        None
                        if item["session_id"] is None
                        else UUID(str(item["session_id"]))
                    ),
                    exercise_id=(
                        None
                        if item["exercise_id"] is None
                        else str(item["exercise_id"])
                    ),
                )
                for item in cast(list[dict[str, Any]], payload["violations"])
            )
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
                occurred_at=MySQLSessionDesignRepository._db_time(at),
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
