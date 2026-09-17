"""MySQL Recovery Application persistence contracts."""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.common import (
    LocationType,
    RepositoryConflictError,
    RepositoryUniqueError,
)
from app.domain.context.enums import ContextDegradedMode
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.recovery_application.enums import RecoveryApplicationOutcome
from app.domain.recovery_application.models import (
    RecoveryApplicationResult,
    RecoveryMemoryProposalImportResult,
)
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
    CalendarEventBindingModel,
    ContextSnapshotModel,
    RecoveryApplicationResultModel,
    RecoveryDraftModel,
    RecoveryMemoryProposalImportModel,
    RecoveryScheduleSubdraftBindingModel,
    RecoverySessionDesignSubdraftBindingModel,
    ScheduleAvailabilityWindowModel,
    ScheduleBusyIntervalModel,
    ScheduleBusySnapshotModel,
    ScheduleCandidateSetModel,
    ScheduleCandidateSlotModel,
    ScheduleDraftModel,
    ScheduleTraceModel,
    SessionDesignCandidateSetModel,
    SessionDesignCandidateSlotModel,
    SessionDesignDraftModel,
    SessionDesignTraceModel,
    SessionExerciseModel,
    UserAccountModel,
    WeeklyPlanModel,
    WorkoutSessionModel,
)
from app.persistence.mysql.plan_repository import MySQLPlanRepository
from app.persistence.mysql.recovery_application_repository import (
    MySQLRecoveryApplicationRepository,
)
from app.persistence.mysql.schedule_repository import MySQLScheduleDraftRepository
from app.persistence.mysql.session_design_repository import MySQLSessionDesignRepository
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.factories import make_plan, make_user
from tests.integration.test_mysql_recovery_draft_persistence import (
    _bundle,
    _run_alembic,
)
from tests.integration.test_mysql_recovery_draft_persistence import (
    _repository as recovery_draft_repository,
)
from tests.integration.test_mysql_recovery_draft_persistence import (
    _save as save_recovery_bundle,
)
from tests.integration.test_mysql_session_design_draft_repository import (
    _candidate as session_candidate,
)
from tests.integration.test_mysql_session_design_draft_repository import (
    _draft as session_draft,
)

pytestmark = pytest.mark.integration


async def _create_draft(database: Database) -> tuple[UUID, UUID, UUID]:
    user = replace(
        make_user(), email=f"recovery-application-{uuid4().hex}@fitweek.test"
    )
    await MySQLUserAccountRepository(database.session_factory).save(user)
    bundle = _bundle(user.id, client_request_id=f"recovery-application-{uuid4().hex}")
    bundle = _with_source_plan_version_one(bundle)
    await save_recovery_bundle(recovery_draft_repository(database), bundle)
    return user.id, bundle.draft.id, bundle.candidate_set.candidates[0].id


def _with_source_plan_version_one(bundle: object) -> object:
    return replace(
        bundle,
        draft=replace(bundle.draft, source_plan_version=1),
        impact=replace(bundle.impact, source_plan_version=1),
        candidate_set=replace(bundle.candidate_set, source_plan_version=1),
    )


async def _create_session_design_child(database: Database, user_id: UUID):
    now = datetime.now(UTC)
    snapshot_id = uuid4()
    async with database.session_factory() as session:
        async with session.begin():
            session.add(
                ContextSnapshotModel(
                    id=str(snapshot_id),
                    user_id=str(user_id),
                    agent_type="SESSION_DESIGNER",
                    scope_id=f"recovery-application-{snapshot_id}",
                    fingerprint="d" * 64,
                    contract_version="context-v1",
                    policy_version="policy-v1",
                    content={"safe_references": []},
                    character_count=0,
                    token_estimate=0,
                    created_at=now,
                )
            )
    candidate = session_candidate(user_id, snapshot_id, now)
    draft, trace = session_draft(candidate, now)
    repository = MySQLSessionDesignRepository(database.session_factory)
    await repository.save_candidate_set(candidate)
    await repository.save_draft(draft, trace)
    accepted = draft.accept(now + timedelta(minutes=1))
    await repository.update_draft(accepted)
    return accepted


async def _create_schedule_child(database: Database, user_id: UUID):
    now = datetime.now(UTC)
    context_id, root_plan_id, session_id = uuid4(), uuid4(), uuid4()
    async with database.session_factory() as session:
        async with session.begin():
            session.add(
                ContextSnapshotModel(
                    id=str(context_id),
                    user_id=str(user_id),
                    agent_type="SCHEDULE_AGENT",
                    scope_id=f"recovery-application-schedule-{context_id}",
                    fingerprint="c" * 64,
                    contract_version="context-v1",
                    policy_version="policy-v1",
                    content={"safe_references": []},
                    character_count=0,
                    token_estimate=0,
                    created_at=now,
                )
            )
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
        context_snapshot_reference_id=context_id,
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
        context_snapshot_reference_id=context_id,
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
        explanation_summary="Controlled Recovery application binding.",
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    trace = ScheduleTrace(
        draft_id=draft.id,
        request_id=draft.request_id,
        busy_snapshot_id=busy.id,
        candidate_set_id=candidate.id,
        candidate_set_fingerprint=candidate.fingerprint,
        context_snapshot_reference_id=context_id,
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
    repository = MySQLScheduleDraftRepository(database.session_factory)
    await repository.save(draft, busy, candidate, trace, availability)
    accepted = draft.accept(now + timedelta(minutes=1))
    await repository.update(accepted)
    return accepted


async def _cleanup(database: Database, user_id: UUID) -> None:
    async with database.session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(RecoveryMemoryProposalImportModel).where(
                    RecoveryMemoryProposalImportModel.user_id == str(user_id)
                )
            )
            await session.execute(
                delete(RecoveryScheduleSubdraftBindingModel).where(
                    RecoveryScheduleSubdraftBindingModel.user_id == str(user_id)
                )
            )
            await session.execute(
                delete(RecoverySessionDesignSubdraftBindingModel).where(
                    RecoverySessionDesignSubdraftBindingModel.user_id == str(user_id)
                )
            )
            draft_ids = (
                await session.scalars(
                    SessionDesignDraftModel.__table__.select()
                    .with_only_columns(SessionDesignDraftModel.id)
                    .where(SessionDesignDraftModel.user_id == str(user_id))
                )
            ).all()
            if draft_ids:
                await session.execute(
                    delete(SessionDesignTraceModel).where(
                        SessionDesignTraceModel.draft_id.in_(draft_ids)
                    )
                )
            await session.execute(
                delete(SessionDesignDraftModel).where(
                    SessionDesignDraftModel.user_id == str(user_id)
                )
            )
            candidate_ids = (
                await session.scalars(
                    SessionDesignCandidateSetModel.__table__.select()
                    .with_only_columns(SessionDesignCandidateSetModel.id)
                    .where(SessionDesignCandidateSetModel.user_id == str(user_id))
                )
            ).all()
            if candidate_ids:
                await session.execute(
                    delete(SessionDesignCandidateSlotModel).where(
                        SessionDesignCandidateSlotModel.candidate_set_id.in_(
                            candidate_ids
                        )
                    )
                )
            await session.execute(
                delete(SessionDesignCandidateSetModel).where(
                    SessionDesignCandidateSetModel.user_id == str(user_id)
                )
            )
            schedule_draft_ids = (
                await session.scalars(
                    select(ScheduleDraftModel.id).where(
                        ScheduleDraftModel.user_id == str(user_id)
                    )
                )
            ).all()
            schedule_candidate_ids = (
                await session.scalars(
                    select(ScheduleCandidateSetModel.id).where(
                        ScheduleCandidateSetModel.user_id == str(user_id)
                    )
                )
            ).all()
            schedule_busy_ids = (
                await session.scalars(
                    select(ScheduleBusySnapshotModel.id).where(
                        ScheduleBusySnapshotModel.user_id == str(user_id)
                    )
                )
            ).all()
            if schedule_draft_ids:
                await session.execute(
                    delete(ScheduleTraceModel).where(
                        ScheduleTraceModel.draft_id.in_(schedule_draft_ids)
                    )
                )
            await session.execute(
                delete(ScheduleDraftModel).where(
                    ScheduleDraftModel.user_id == str(user_id)
                )
            )
            if schedule_candidate_ids:
                await session.execute(
                    delete(ScheduleCandidateSlotModel).where(
                        ScheduleCandidateSlotModel.candidate_set_id.in_(
                            schedule_candidate_ids
                        )
                    )
                )
                await session.execute(
                    delete(ScheduleAvailabilityWindowModel).where(
                        ScheduleAvailabilityWindowModel.candidate_set_id.in_(
                            schedule_candidate_ids
                        )
                    )
                )
            await session.execute(
                delete(ScheduleCandidateSetModel).where(
                    ScheduleCandidateSetModel.user_id == str(user_id)
                )
            )
            if schedule_busy_ids:
                await session.execute(
                    delete(ScheduleBusyIntervalModel).where(
                        ScheduleBusyIntervalModel.snapshot_id.in_(schedule_busy_ids)
                    )
                )
            await session.execute(
                delete(ScheduleBusySnapshotModel).where(
                    ScheduleBusySnapshotModel.user_id == str(user_id)
                )
            )
            await session.execute(
                delete(ContextSnapshotModel).where(
                    ContextSnapshotModel.user_id == str(user_id)
                )
            )
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id == str(user_id))
            )
            await session.execute(
                delete(RecoveryApplicationResultModel).where(
                    RecoveryApplicationResultModel.user_id == str(user_id)
                )
            )
            for table_name in ("recovery_trace", "recovery_memory_proposal"):
                await session.execute(
                    text(f"DELETE FROM {table_name} WHERE user_id = :user_id"),
                    {"user_id": str(user_id)},
                )
            await session.execute(
                delete(RecoveryDraftModel).where(
                    RecoveryDraftModel.user_id == str(user_id)
                )
            )
            for table_name in (
                "recovery_action_candidate",
                "recovery_candidate_set",
                "recovery_change_impact",
                "recovery_behavior_summary",
            ):
                await session.execute(
                    text(f"DELETE FROM {table_name} WHERE user_id = :user_id"),
                    {"user_id": str(user_id)},
                )
            await session.execute(
                text(
                    "DELETE session_exercise FROM session_exercise "
                    "JOIN workout_session ON session_exercise.session_id = workout_session.id "
                    "JOIN weekly_plan ON workout_session.plan_id = weekly_plan.id "
                    "WHERE weekly_plan.user_id = :user_id"
                ),
                {"user_id": str(user_id)},
            )
            await session.execute(
                text(
                    "DELETE workout_session FROM workout_session "
                    "JOIN weekly_plan ON workout_session.plan_id = weekly_plan.id "
                    "WHERE weekly_plan.user_id = :user_id"
                ),
                {"user_id": str(user_id)},
            )
            await session.execute(
                delete(WeeklyPlanModel).where(WeeklyPlanModel.user_id == str(user_id))
            )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id == str(user_id))
            )


@pytest.mark.asyncio
async def test_mysql_recovery_application_migration_round_trip_preserves_0011(
    mysql_test_database: Database,
    mysql_test_url: str,
) -> None:
    """Head contains exactly the four Recovery Application persistence tables."""

    preservation_user_id: UUID | None = None
    _run_alembic(mysql_test_url, "upgrade", "0012_recovery_application_persistence")
    try:
        preservation_user = replace(
            make_user(), email=f"recovery-migration-{uuid4().hex}@fitweek.test"
        )
        preservation_user_id = preservation_user.id
        await MySQLUserAccountRepository(mysql_test_database.session_factory).save(
            preservation_user
        )
        preservation_bundle = _with_source_plan_version_one(
            _bundle(
                preservation_user.id,
                client_request_id=f"recovery-migration-{uuid4().hex}",
            )
        )
        await save_recovery_bundle(
            recovery_draft_repository(mysql_test_database), preservation_bundle
        )
        calendar_binding_id = uuid4()
        async with mysql_test_database.session_factory() as session:
            async with session.begin():
                session.add(
                    CalendarEventBindingModel(
                        id=str(calendar_binding_id),
                        user_id=str(preservation_user.id),
                        provider="migration-test",
                        calendar_id="primary",
                        root_plan_id=str(preservation_bundle.draft.root_plan_id),
                        session_id=str(uuid4()),
                        external_event_id=f"event-{calendar_binding_id}",
                        stable_uid=f"uid-{calendar_binding_id}",
                        last_payload_fingerprint="a" * 64,
                        status="ACTIVE",
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                        version=1,
                    )
                )
        async with mysql_test_database.engine.connect() as connection:

            def schema(sync_connection: object) -> tuple[object, ...]:
                inspector = inspect(sync_connection)
                result_columns = {
                    item["name"]: item
                    for item in inspector.get_columns("recovery_application_result")
                }
                result_foreign_keys = inspector.get_foreign_keys(
                    "recovery_application_result"
                )
                return (
                    set(inspector.get_table_names()),
                    result_columns,
                    {
                        item["name"]
                        for item in inspector.get_indexes("recovery_application_result")
                    },
                    {
                        item["name"]
                        for item in inspector.get_unique_constraints(
                            "recovery_application_result"
                        )
                    },
                    result_foreign_keys,
                )

            (
                table_names,
                result_columns,
                indexes,
                unique_keys,
                foreign_keys,
            ) = await connection.run_sync(schema)
        assert {
            "recovery_application_result",
            "recovery_session_design_subdraft_binding",
            "recovery_schedule_subdraft_binding",
            "recovery_memory_proposal_import",
        } <= table_names
        assert result_columns["id"]["type"].length == 36
        assert result_columns["user_id"]["type"].length == 36
        assert result_columns["client_request_id"]["type"].length == 128
        assert result_columns["application_fingerprint"]["type"].length == 64
        assert result_columns["request_fingerprint"]["type"].length == 64
        assert result_columns["created_at"]["type"].fsp == 6
        assert result_columns["created_revision"]["nullable"] is True
        assert result_columns["applied_action_candidate_ids"]["nullable"] is False
        assert {
            "ix_recovery_application_user_result",
            "ix_recovery_application_user_draft",
        } <= indexes
        assert {
            "uq_recovery_application_request",
            "uq_recovery_application_draft",
        } <= unique_keys
        assert {
            (item["constrained_columns"][0], item["referred_table"])
            for item in foreign_keys
        } == {("user_id", "user_account"), ("recovery_draft_id", "recovery_draft")}
        _run_alembic(mysql_test_url, "downgrade", "0011_recovery_draft_persistence")
        async with mysql_test_database.session_factory() as session:
            assert (
                await session.scalar(
                    select(RecoveryDraftModel.id).where(
                        RecoveryDraftModel.id == str(preservation_bundle.draft.id)
                    )
                )
            ) == str(preservation_bundle.draft.id)
            assert (
                await session.scalar(
                    select(CalendarEventBindingModel.id).where(
                        CalendarEventBindingModel.id == str(calendar_binding_id)
                    )
                )
            ) == str(calendar_binding_id)
        async with mysql_test_database.engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
        assert "recovery_application_result" not in table_names
        _run_alembic(mysql_test_url, "upgrade", "0012_recovery_application_persistence")
    finally:
        _run_alembic(mysql_test_url, "upgrade", "head")
        if preservation_user_id is not None:
            async with mysql_test_database.session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(CalendarEventBindingModel).where(
                            CalendarEventBindingModel.user_id
                            == str(preservation_user_id)
                        )
                    )
            await _cleanup(mysql_test_database, (preservation_user_id,))


@pytest.mark.asyncio
async def test_mysql_recovery_application_repository_round_trips_result_bindings_and_memory_import(
    mysql_test_database: Database,
) -> None:
    """Memory-import durable replay is user-scoped and reconstructs UTC tuples."""

    user_id, draft_id, candidate_id = await _create_draft(mysql_test_database)
    other_user_id, _, _ = await _create_draft(mysql_test_database)
    foreign_import_id = uuid4()
    try:
        repository = MySQLRecoveryApplicationRepository(
            mysql_test_database.session_factory
        )
        receipt = RecoveryMemoryProposalImportResult(
            id=uuid4(),
            user_id=user_id,
            draft_id=draft_id,
            client_request_id=f"memory-import-{uuid4().hex}",
            fingerprint="f" * 64,
            proposal_ids=(uuid4(),),
            memory_candidate_ids=(uuid4(),),
            created_at=datetime.now(UTC),
        )
        saved = await repository.save_memory_import(receipt)
        replay = await MySQLRecoveryApplicationRepository(
            mysql_test_database.session_factory
        ).get_memory_import(user_id, receipt.client_request_id)
        assert saved == receipt
        assert replay == receipt
        assert (
            await repository.get_memory_import(uuid4(), receipt.client_request_id)
            is None
        )
        with pytest.raises(RepositoryConflictError):
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).save_memory_import(
                replace(
                    receipt,
                    id=foreign_import_id,
                    user_id=other_user_id,
                    client_request_id=f"foreign-draft-import-{uuid4().hex}",
                )
            )
        child_draft = await _create_session_design_child(mysql_test_database, user_id)
        await repository.bind_session_design_subdraft(
            draft_id, candidate_id, child_draft.id
        )
        await repository.bind_session_design_subdraft(
            draft_id, candidate_id, child_draft.id
        )
        assert (
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).get_session_design_subdraft(draft_id, candidate_id)
            == child_draft.id
        )
        with pytest.raises(RepositoryUniqueError):
            await repository.bind_session_design_subdraft(
                draft_id,
                candidate_id,
                (await _create_session_design_child(mysql_test_database, user_id)).id,
            )
        schedule_child = await _create_schedule_child(mysql_test_database, user_id)
        await repository.bind_schedule_subdraft(
            draft_id, candidate_id, schedule_child.id
        )
        await repository.bind_schedule_subdraft(
            draft_id, candidate_id, schedule_child.id
        )
        assert (
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).get_schedule_subdraft(draft_id, candidate_id)
            == schedule_child.id
        )
    finally:
        async with mysql_test_database.session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(RecoveryMemoryProposalImportModel).where(
                        RecoveryMemoryProposalImportModel.id == str(foreign_import_id)
                    )
                )
        await _cleanup(mysql_test_database, user_id)
        await _cleanup(mysql_test_database, other_user_id)


@pytest.mark.asyncio
async def test_mysql_recovery_application_commit_is_atomic_cas_and_reconstructs_plan_children(
    mysql_test_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-change application atomically records the durable applied Draft fact."""

    user = replace(make_user(), email=f"recovery-commit-{uuid4().hex}@fitweek.test")
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
    try:
        bundle = _bundle(user.id, client_request_id=f"recovery-commit-{uuid4().hex}")
        bundle = _with_source_plan_version_one(bundle)
        source_base = make_plan(user_id=user.id, status=WeeklyPlanStatus.CONFIRMED)
        source = replace(
            source_base,
            id=bundle.draft.root_plan_id,
            revision=bundle.draft.source_revision,
            sessions=tuple(
                replace(item, plan_id=bundle.draft.root_plan_id)
                for item in source_base.sessions
            ),
        )
        await MySQLPlanRepository(mysql_test_database.session_factory).save(source)
        pending = await save_recovery_bundle(
            recovery_draft_repository(mysql_test_database), bundle
        )
        accepted = pending.accept(
            expected_version=pending.version,
            at=pending.created_at + timedelta(minutes=1),
        )
        accepted = await recovery_draft_repository(mysql_test_database).update_draft(
            accepted
        )
        result = RecoveryApplicationResult(
            id=uuid4(),
            user_id=user.id,
            client_request_id=f"apply-{uuid4().hex}",
            application_fingerprint="a" * 64,
            request_fingerprint="b" * 64,
            recovery_draft_id=accepted.id,
            root_plan_id=source.id,
            source_revision=source.revision,
            created_revision=None,
            applied_action_candidate_ids=(),
            session_design_draft_ids=(),
            schedule_draft_ids=(),
            affected_session_ids=(),
            removed_session_ids=(),
            preserved_session_ids=(),
            immutable_session_ids=(),
            outcome=RecoveryApplicationOutcome.NO_CHANGE,
            created_at=accepted.created_at + timedelta(minutes=2),
        )
        applied = accepted.mark_applied(
            application_result_id=result.id,
            root_plan_id=source.id,
            source_revision=source.revision,
            created_revision=None,
            affected_session_ids=(),
            session_design_draft_ids=(),
            schedule_draft_ids=(),
            at=result.created_at,
        )
        committed = await MySQLRecoveryApplicationRepository(
            mysql_test_database.session_factory
        ).commit(
            source=source,
            expected_draft=accepted,
            applied_draft=applied,
            revision=None,
            result=result,
            applied_session_design_drafts=(),
            applied_schedule_drafts=(),
        )
        restored = await MySQLRecoveryApplicationRepository(
            mysql_test_database.session_factory
        ).get_result_by_draft(user.id, accepted.id)
        rebuilt = MySQLRecoveryApplicationRepository(
            mysql_test_database.session_factory
        )
        assert await rebuilt.get_result(user.id, result.id) == result
        assert (
            await rebuilt.get_result_by_request(user.id, result.client_request_id)
            == result
        )
        assert await rebuilt.get_result(uuid4(), result.id) is None
        assert (
            await rebuilt.get_result_by_request(uuid4(), result.client_request_id)
            is None
        )
        replay = await MySQLRecoveryApplicationRepository(
            mysql_test_database.session_factory
        ).commit(
            source=source,
            expected_draft=accepted,
            applied_draft=applied,
            revision=None,
            result=result,
            applied_session_design_drafts=(),
            applied_schedule_drafts=(),
        )
        assert committed.created is True
        assert committed.result == result
        assert committed.plan_revision_id is None
        assert restored == result
        assert replay.created is False
        assert replay.result == result
        with pytest.raises(RepositoryUniqueError):
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).commit(
                source=source,
                expected_draft=accepted,
                applied_draft=applied,
                revision=None,
                result=replace(result, affected_session_ids=(uuid4(),)),
                applied_session_design_drafts=(),
                applied_schedule_drafts=(),
            )

        full_bundle = _with_source_plan_version_one(
            _bundle(user.id, client_request_id=f"recovery-full-{uuid4().hex}")
        )
        full_source_base = make_plan(user_id=user.id, status=WeeklyPlanStatus.CONFIRMED)
        full_source = replace(
            full_source_base,
            id=full_bundle.draft.root_plan_id,
            revision=full_bundle.draft.source_revision,
            sessions=tuple(
                replace(item, plan_id=full_bundle.draft.root_plan_id)
                for item in full_source_base.sessions
            ),
        )
        await MySQLPlanRepository(mysql_test_database.session_factory).save(full_source)
        pending_full = await save_recovery_bundle(
            recovery_draft_repository(mysql_test_database), full_bundle
        )
        accepted_full = await recovery_draft_repository(
            mysql_test_database
        ).update_draft(
            pending_full.accept(
                expected_version=pending_full.version,
                at=pending_full.created_at + timedelta(minutes=1),
            )
        )
        design_child = await _create_session_design_child(mysql_test_database, user.id)
        schedule_child = await _create_schedule_child(mysql_test_database, user.id)
        revision = replace(
            full_source,
            id=uuid4(),
            root_plan_id=full_source.id,
            revision=full_source.revision + 1,
        )
        full_result = RecoveryApplicationResult(
            id=uuid4(),
            user_id=user.id,
            client_request_id=f"apply-full-{uuid4().hex}",
            application_fingerprint="c" * 64,
            request_fingerprint="d" * 64,
            recovery_draft_id=accepted_full.id,
            root_plan_id=full_source.id,
            source_revision=full_source.revision,
            created_revision=revision.revision,
            applied_action_candidate_ids=(),
            session_design_draft_ids=(design_child.id,),
            schedule_draft_ids=(schedule_child.id,),
            affected_session_ids=(revision.sessions[0].id,),
            removed_session_ids=(),
            preserved_session_ids=(),
            immutable_session_ids=(),
            outcome=RecoveryApplicationOutcome.PLAN_REVISION_CREATED,
            created_at=accepted_full.created_at + timedelta(minutes=2),
        )
        applied_full = accepted_full.mark_applied(
            application_result_id=full_result.id,
            root_plan_id=full_source.id,
            source_revision=full_source.revision,
            created_revision=revision.revision,
            affected_session_ids=full_result.affected_session_ids,
            session_design_draft_ids=full_result.session_design_draft_ids,
            schedule_draft_ids=full_result.schedule_draft_ids,
            at=full_result.created_at,
        )
        applied_design = design_child.mark_applied(
            root_plan_id=full_source.id,
            revision=revision.revision,
            session_id=revision.sessions[0].id,
            application_result_id=full_result.id,
            at=full_result.created_at,
        )
        applied_schedule = schedule_child.mark_applied(
            root_plan_id=full_source.id,
            source_revision=full_source.revision,
            created_revision=revision.revision,
            application_result_id=full_result.id,
            at=full_result.created_at,
        )
        with pytest.raises(RepositoryConflictError):
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).commit(
                source=replace(full_source, version=full_source.version + 1),
                expected_draft=accepted_full,
                applied_draft=applied_full,
                revision=revision,
                result=full_result,
                applied_session_design_drafts=(applied_design,),
                applied_schedule_drafts=(applied_schedule,),
            )
        with pytest.raises(RepositoryConflictError):
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).commit(
                source=full_source,
                expected_draft=accepted_full,
                applied_draft=applied_full,
                revision=revision,
                result=full_result,
                applied_session_design_drafts=(
                    replace(applied_design, version=applied_design.version + 1),
                ),
                applied_schedule_drafts=(applied_schedule,),
            )
        with pytest.raises(RepositoryConflictError):
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).commit(
                source=full_source,
                expected_draft=accepted_full,
                applied_draft=applied_full,
                revision=revision,
                result=full_result,
                applied_session_design_drafts=(applied_design,),
                applied_schedule_drafts=(
                    replace(applied_schedule, version=applied_schedule.version + 1),
                ),
            )
        original_flush = AsyncSession.flush
        flush_count = 0

        async def fail_result_flush(
            instance: AsyncSession, *args: object, **kwargs: object
        ) -> None:
            nonlocal flush_count
            flush_count += 1
            if flush_count == len(revision.sessions) + 2:
                raise RuntimeError(
                    "injected Recovery application post-write flush failure"
                )
            await original_flush(instance, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "flush", fail_result_flush)
        with pytest.raises(RuntimeError, match="post-write flush failure"):
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).commit(
                source=full_source,
                expected_draft=accepted_full,
                applied_draft=applied_full,
                revision=revision,
                result=full_result,
                applied_session_design_drafts=(applied_design,),
                applied_schedule_drafts=(applied_schedule,),
            )
        monkeypatch.setattr(AsyncSession, "flush", original_flush)
        async with mysql_test_database.session_factory() as session:
            assert (
                await session.scalar(
                    select(RecoveryApplicationResultModel.id).where(
                        RecoveryApplicationResultModel.id == str(full_result.id)
                    )
                )
                is None
            )
            assert (
                await session.scalar(
                    select(WeeklyPlanModel.id).where(
                        WeeklyPlanModel.id == str(revision.id)
                    )
                )
                is None
            )
            assert (
                await session.scalar(
                    select(RecoveryDraftModel.status).where(
                        RecoveryDraftModel.id == str(accepted_full.id)
                    )
                )
                == "ACCEPTED"
            )
            assert (
                await session.scalar(
                    select(SessionDesignDraftModel.status).where(
                        SessionDesignDraftModel.id == str(design_child.id)
                    )
                )
                == "ACCEPTED"
            )
            assert (
                await session.scalar(
                    select(ScheduleDraftModel.status).where(
                        ScheduleDraftModel.id == str(schedule_child.id)
                    )
                )
                == "ACCEPTED"
            )
        full_commit = await MySQLRecoveryApplicationRepository(
            mysql_test_database.session_factory
        ).commit(
            source=full_source,
            expected_draft=accepted_full,
            applied_draft=applied_full,
            revision=revision,
            result=full_result,
            applied_session_design_drafts=(applied_design,),
            applied_schedule_drafts=(applied_schedule,),
        )
        assert full_commit.created is True
        assert full_commit.plan_revision_id == revision.id
        with pytest.raises(RepositoryConflictError):
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).commit(
                source=full_source,
                expected_draft=accepted_full,
                applied_draft=applied_full,
                revision=revision,
                result=replace(
                    full_result,
                    id=uuid4(),
                    client_request_id=f"stale-apply-{uuid4().hex}",
                    application_fingerprint="e" * 64,
                ),
                applied_session_design_drafts=(applied_design,),
                applied_schedule_drafts=(applied_schedule,),
            )
        async with mysql_test_database.session_factory() as session:
            assert await session.scalar(
                select(WeeklyPlanModel.id).where(WeeklyPlanModel.id == str(revision.id))
            ) == str(revision.id)
            revision_sessions = (
                await session.scalars(
                    select(WorkoutSessionModel.id).where(
                        WorkoutSessionModel.plan_id == str(revision.id)
                    )
                )
            ).all()
            assert len(revision_sessions) == len(revision.sessions)
            assert (
                await session.scalar(
                    select(WeeklyPlanModel.version).where(
                        WeeklyPlanModel.id == str(full_source.id)
                    )
                )
                == full_source.version
            )
            assert len(
                (
                    await session.scalars(
                        select(SessionExerciseModel.session_id).where(
                            SessionExerciseModel.session_id.in_(revision_sessions)
                        )
                    )
                ).all()
            ) == sum(len(item.exercises) for item in revision.sessions)
            assert (
                await session.scalar(
                    select(SessionDesignDraftModel.status).where(
                        SessionDesignDraftModel.id == str(design_child.id)
                    )
                )
                == "APPLIED"
            )
        with pytest.raises(RepositoryUniqueError):
            await MySQLPlanRepository(mysql_test_database.session_factory).save(
                replace(revision, id=uuid4())
            )
            assert (
                await session.scalar(
                    select(ScheduleDraftModel.status).where(
                        ScheduleDraftModel.id == str(schedule_child.id)
                    )
                )
                == "APPLIED"
            )
    finally:
        await _cleanup(mysql_test_database, user.id)


@pytest.mark.asyncio
async def test_mysql_recovery_application_concurrent_idempotency_and_restart_recovery(
    mysql_test_database: Database,
) -> None:
    """The Repository exposes durable request winner and binding/import methods."""

    user_id, draft_id, candidate_id = await _create_draft(mysql_test_database)
    race_user = replace(make_user(), email=f"recovery-race-{uuid4().hex}@fitweek.test")
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(
        race_user
    )
    try:
        receipt = RecoveryMemoryProposalImportResult(
            id=uuid4(),
            user_id=user_id,
            draft_id=draft_id,
            client_request_id=f"concurrent-import-{uuid4().hex}",
            fingerprint="c" * 64,
            proposal_ids=(uuid4(),),
            memory_candidate_ids=(uuid4(),),
            created_at=datetime.now(UTC),
        )
        first, second = await asyncio.gather(
            MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).save_memory_import(receipt),
            MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).save_memory_import(receipt),
        )
        assert first == receipt
        assert second == receipt
        with pytest.raises(RepositoryUniqueError):
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).save_memory_import(replace(receipt, fingerprint="e" * 64))
        design_child = await _create_session_design_child(mysql_test_database, user_id)
        await asyncio.gather(
            MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).bind_session_design_subdraft(draft_id, candidate_id, design_child.id),
            MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).bind_session_design_subdraft(draft_id, candidate_id, design_child.id),
        )
        assert (
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).get_session_design_subdraft(draft_id, candidate_id)
            == design_child.id
        )
        with pytest.raises(RepositoryUniqueError):
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).bind_session_design_subdraft(
                draft_id,
                candidate_id,
                (await _create_session_design_child(mysql_test_database, user_id)).id,
            )
        schedule_child = await _create_schedule_child(mysql_test_database, user_id)
        await asyncio.gather(
            MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).bind_schedule_subdraft(draft_id, candidate_id, schedule_child.id),
            MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).bind_schedule_subdraft(draft_id, candidate_id, schedule_child.id),
        )
        assert (
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).get_schedule_subdraft(draft_id, candidate_id)
            == schedule_child.id
        )
        with pytest.raises(IntegrityError):
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).save_memory_import(
                replace(
                    receipt,
                    client_request_id=f"colliding-import-{uuid4().hex}",
                    fingerprint="d" * 64,
                )
            )

        race_bundle = _with_source_plan_version_one(
            _bundle(race_user.id, client_request_id=f"recovery-race-{uuid4().hex}")
        )
        race_source_base = make_plan(
            user_id=race_user.id, status=WeeklyPlanStatus.CONFIRMED
        )
        race_source = replace(
            race_source_base,
            id=race_bundle.draft.root_plan_id,
            revision=race_bundle.draft.source_revision,
            sessions=tuple(
                replace(item, plan_id=race_bundle.draft.root_plan_id)
                for item in race_source_base.sessions
            ),
        )
        await MySQLPlanRepository(mysql_test_database.session_factory).save(race_source)
        race_pending = await save_recovery_bundle(
            recovery_draft_repository(mysql_test_database), race_bundle
        )
        race_accepted = await recovery_draft_repository(
            mysql_test_database
        ).update_draft(
            race_pending.accept(
                expected_version=race_pending.version,
                at=race_pending.created_at + timedelta(minutes=1),
            )
        )
        race_result = RecoveryApplicationResult(
            id=uuid4(),
            user_id=race_user.id,
            client_request_id=f"apply-race-{uuid4().hex}",
            application_fingerprint="a" * 64,
            request_fingerprint="b" * 64,
            recovery_draft_id=race_accepted.id,
            root_plan_id=race_source.id,
            source_revision=race_source.revision,
            created_revision=None,
            applied_action_candidate_ids=(),
            session_design_draft_ids=(),
            schedule_draft_ids=(),
            affected_session_ids=(),
            removed_session_ids=(),
            preserved_session_ids=(),
            immutable_session_ids=(),
            outcome=RecoveryApplicationOutcome.NO_CHANGE,
            created_at=race_accepted.created_at + timedelta(minutes=2),
        )
        race_applied = race_accepted.mark_applied(
            application_result_id=race_result.id,
            root_plan_id=race_source.id,
            source_revision=race_source.revision,
            created_revision=None,
            affected_session_ids=(),
            session_design_draft_ids=(),
            schedule_draft_ids=(),
            at=race_result.created_at,
        )

        async def race_commit() -> object:
            return await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).commit(
                source=race_source,
                expected_draft=race_accepted,
                applied_draft=race_applied,
                revision=None,
                result=race_result,
                applied_session_design_drafts=(),
                applied_schedule_drafts=(),
            )

        first_commit, second_commit = await asyncio.gather(race_commit(), race_commit())
        assert {first_commit.created, second_commit.created} == {False, True}
        assert first_commit.result == race_result
        assert second_commit.result == race_result
        assert (
            await MySQLRecoveryApplicationRepository(
                mysql_test_database.session_factory
            ).get_result_by_request(race_user.id, race_result.client_request_id)
            == race_result
        )
    finally:
        await _cleanup(mysql_test_database, user_id)
        await _cleanup(mysql_test_database, race_user.id)
