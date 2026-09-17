"""MySQL repository contract for frozen candidates and Session Design Drafts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete

from app.domain.common import (
    LocationType,
    RepositoryConflictError,
)
from app.domain.context.enums import ContextDegradedMode
from app.domain.profiles.models import FitnessGoal
from app.domain.session_design.enums import (
    SessionDesignSource,
    SessionExerciseRole,
    SessionTemplateId,
)
from app.domain.session_design.models import (
    CandidateSlot,
    ExerciseCandidateSet,
    SessionDesignDraft,
    SessionDesignTrace,
)
from app.domain.sessions.models import SessionExercise, SessionType
from app.persistence.database import Database
from app.persistence.mysql.models import (
    AuditEventModel,
    ContextSnapshotModel,
    SessionDesignCandidateSetModel,
    SessionDesignCandidateSlotModel,
    SessionDesignDraftModel,
    SessionDesignTraceModel,
    UserAccountModel,
)
from app.persistence.mysql.session_design_repository import (
    MySQLSessionDesignRepository,
)
from app.safety.models import SafetyValidationResult
from app.session_design.duration import SessionDurationPolicy


async def _seed_owner(
    database: Database,
    user_id: UUID,
    snapshot_id: UUID,
    now: datetime,
) -> None:
    async with database.session_factory() as session:
        async with session.begin():
            session.add(
                UserAccountModel(
                    id=str(user_id),
                    email=f"session-design-repository-{user_id.hex}@fitweek.test",
                    display_name="Session Design Repository",
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
                    agent_type="SESSION_DESIGNER",
                    scope_id=f"session-design-repository-{snapshot_id}",
                    fingerprint="c" * 64,
                    contract_version="context-v1",
                    policy_version="policy-v1",
                    content={"safe_references": []},
                    character_count=0,
                    token_estimate=0,
                    created_at=now.replace(tzinfo=None),
                )
            )


def _candidate(
    user_id: UUID,
    snapshot_id: UUID,
    now: datetime,
) -> ExerciseCandidateSet:
    return ExerciseCandidateSet(
        id=uuid4(),
        user_id=user_id,
        request_fingerprint="a" * 64,
        fingerprint="b" * 64,
        template_id=SessionTemplateId.FULL_BODY_BASIC,
        template_version="session-template-v1",
        catalog_version="catalog-v1",
        context_snapshot_reference_id=snapshot_id,
        context_fingerprint="c" * 64,
        slots=(
            CandidateSlot(
                slot_id="warmup",
                role=SessionExerciseRole.WARMUP,
                exercise_ids=("march_in_place",),
            ),
            CandidateSlot(
                slot_id="main",
                role=SessionExerciseRole.MAIN,
                exercise_ids=("bodyweight_squat",),
            ),
            CandidateSlot(
                slot_id="cooldown",
                role=SessionExerciseRole.COOLDOWN,
                exercise_ids=("standing_quad_stretch",),
            ),
        ),
        created_at=now,
    )


def _draft(
    candidate: ExerciseCandidateSet,
    now: datetime,
) -> tuple[SessionDesignDraft, SessionDesignTrace]:
    exercises, duration = SessionDurationPolicy().fit_exact(
        tuple(
            SessionExercise(
                exercise_id=slot.exercise_ids[0],
                sequence_no=index,
                sets=None,
                repetitions=None,
                duration_seconds=60,
                rest_seconds=10,
            )
            for index, slot in enumerate(candidate.slots, start=1)
        ),
        30,
    )
    draft = SessionDesignDraft(
        id=uuid4(),
        request_id=uuid4(),
        client_request_id=f"repository-{uuid4().hex}",
        user_id=candidate.user_id,
        request_payload_fingerprint="d" * 64,
        candidate_set_id=candidate.id,
        candidate_set_fingerprint=candidate.fingerprint,
        context_snapshot_reference_id=candidate.context_snapshot_reference_id,
        context_fingerprint=candidate.context_fingerprint,
        context_degraded_mode=ContextDegradedMode.NONE,
        template_id=candidate.template_id,
        template_version=candidate.template_version,
        catalog_version=candidate.catalog_version,
        session_type=SessionType.MIXED,
        target_date=(now + timedelta(days=7)).date(),
        target_duration_minutes=30,
        location=LocationType.HOME,
        goal=FitnessGoal.GENERAL_FITNESS,
        exercises=exercises,
        exercise_roles=tuple(item.role for item in candidate.slots),
        duration=duration,
        safety_validation=SafetyValidationResult.from_violations(()),
        source=SessionDesignSource.TEMPLATE_FALLBACK,
        prompt_version="session-designer-v1",
        provider_summary="template-fallback",
        fallback_used=True,
        explanation_summary="Controlled repository round trip.",
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    return draft, SessionDesignTrace(
        draft_id=draft.id,
        request_id=draft.request_id,
        candidate_set_id=candidate.id,
        candidate_set_fingerprint=candidate.fingerprint,
        context_snapshot_reference_id=candidate.context_snapshot_reference_id,
        context_fingerprint=candidate.context_fingerprint,
        prompt_version=draft.prompt_version,
        template_id=draft.template_id,
        template_version=draft.template_version,
        provider_summary=draft.provider_summary,
        source=draft.source,
        fallback_used=draft.fallback_used,
        validation_error_code=None,
        model_trace_ids=(),
    )


async def _cleanup(database: Database, user_id: UUID) -> None:
    owner = str(user_id)
    async with database.session_factory() as session:
        async with session.begin():
            draft_ids = await session.scalars(
                SessionDesignDraftModel.__table__.select()
                .with_only_columns(SessionDesignDraftModel.id)
                .where(SessionDesignDraftModel.user_id == owner)
            )
            await session.execute(
                delete(SessionDesignTraceModel).where(
                    SessionDesignTraceModel.draft_id.in_(draft_ids.all())
                )
            )
            await session.execute(
                delete(SessionDesignDraftModel).where(
                    SessionDesignDraftModel.user_id == owner
                )
            )
            candidate_ids = await session.scalars(
                SessionDesignCandidateSetModel.__table__.select()
                .with_only_columns(SessionDesignCandidateSetModel.id)
                .where(SessionDesignCandidateSetModel.user_id == owner)
            )
            await session.execute(
                delete(SessionDesignCandidateSlotModel).where(
                    SessionDesignCandidateSlotModel.candidate_set_id.in_(
                        candidate_ids.all()
                    )
                )
            )
            await session.execute(
                delete(SessionDesignCandidateSetModel).where(
                    SessionDesignCandidateSetModel.user_id == owner
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
async def test_mysql_frozen_candidate_and_draft_round_trip_review_and_locking(
    mysql_test_database: Database,
) -> None:
    now = datetime.now(UTC).replace(microsecond=123456)
    user_id, snapshot_id = uuid4(), uuid4()
    await _seed_owner(mysql_test_database, user_id, snapshot_id, now)
    repository = MySQLSessionDesignRepository(mysql_test_database.session_factory)
    candidate = _candidate(user_id, snapshot_id, now)
    draft, trace = _draft(candidate, now)
    try:
        assert await repository.save_candidate_set(candidate) == candidate
        assert await repository.save_draft(draft, trace) == draft
        rebuilt = MySQLSessionDesignRepository(mysql_test_database.session_factory)
        assert await rebuilt.get_candidate_set(user_id, candidate.id) == candidate
        assert await rebuilt.get_draft(user_id, draft.id) == draft
        assert await rebuilt.get_trace(user_id, draft.id) == trace
        assert await rebuilt.get_draft(uuid4(), draft.id) is None
        accepted = draft.accept(now + timedelta(minutes=1))
        assert await rebuilt.update_draft(accepted) == accepted
        with pytest.raises(RepositoryConflictError):
            await rebuilt.update_draft(draft.reject(now + timedelta(minutes=2)))
        assert await rebuilt.get_draft(user_id, draft.id) == accepted
    finally:
        await _cleanup(mysql_test_database, user_id)
