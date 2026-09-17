"""MySQL persistence contracts for the complete Recovery Draft artifact bundle."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import delete, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.behavior.enums import (
    BehaviorConfidenceTier,
    BehaviorMemoryProposalStatus,
    BehaviorPatternType,
)
from app.domain.behavior.models import (
    BehaviorEvidenceReference,
    BehaviorMemoryProposal,
    BehaviorPattern,
    BehaviorSummary,
)
from app.domain.checkins.models import CheckInStatus
from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.memory.enums import MemoryType
from app.domain.recovery.enums import (
    RecoveryActionType,
    RecoveryDraftOutcome,
    RecoveryDraftSource,
    RecoveryDraftStatus,
    RecoveryRedesignGoal,
    RecoveryScopeStatus,
)
from app.domain.recovery.models import (
    RecoveryActionCandidate,
    RecoveryActionCandidateSet,
    RecoveryChangeImpactSnapshot,
    RecoveryDraft,
    RecoveryTrace,
)
from app.persistence.database import Database
from app.persistence.mysql.models import (
    Base,
    RecoveryBehaviorSummaryModel,
    RecoveryDraftModel,
    UserAccountModel,
)
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from tests.factories import make_user

try:
    from app.persistence.mysql.recovery_draft_repository import (
        MySQLRecoveryDraftRepository,
    )
except ModuleNotFoundError:
    MySQLRecoveryDraftRepository = None  # type: ignore[assignment,misc]


NOW = datetime(2026, 7, 30, 9, 15, 27, 123456, tzinfo=UTC)
RECOVERY_TABLES = (
    "recovery_trace",
    "recovery_memory_proposal",
    "recovery_draft",
    "recovery_action_candidate",
    "recovery_candidate_set",
    "recovery_change_impact",
    "recovery_behavior_summary",
)


@dataclass(frozen=True, slots=True)
class RecoveryBundle:
    draft: RecoveryDraft
    summary: BehaviorSummary
    impact: RecoveryChangeImpactSnapshot
    candidate_set: RecoveryActionCandidateSet
    proposals: tuple[BehaviorMemoryProposal, ...]
    trace: RecoveryTrace


def _repository(database: Database) -> MySQLRecoveryDraftRepository:
    assert MySQLRecoveryDraftRepository is not None, (
        "MySQLRecoveryDraftRepository is not implemented"
    )
    return MySQLRecoveryDraftRepository(database.session_factory)


def _sorted_ids(*values: UUID) -> tuple[UUID, ...]:
    return tuple(sorted(values, key=str))


def _bundle(
    user_id: UUID,
    *,
    client_request_id: str | None = None,
    request_payload_fingerprint: str = "1" * 64,
) -> RecoveryBundle:
    root_plan_id = uuid4()
    request_id = uuid4()
    context_id = uuid4()
    evidence_ids = _sorted_ids(uuid4(), uuid4(), uuid4())
    logical_sessions = _sorted_ids(uuid4(), uuid4(), uuid4())
    evidence = tuple(
        BehaviorEvidenceReference(
            checkin_id=checkin_id,
            logical_session_id=logical_sessions[index],
            root_plan_id=root_plan_id,
            plan_revision=3,
            scheduled_at_utc=NOW - timedelta(days=10 - index),
            status=(
                CheckInStatus.COMPLETED
                if index == 0
                else (
                    CheckInStatus.PARTIALLY_COMPLETED
                    if index == 1
                    else CheckInStatus.SKIPPED
                )
            ),
            reported_rpe=(7 + index if index < 2 else None),
            occurred_at=NOW - timedelta(days=10 - index, minutes=-30),
            scheduled_weekday=("MONDAY", "TUESDAY", "WEDNESDAY")[index],
            scheduled_time_bucket=("MORNING", "EVENING", "MORNING")[index],
            location=("HOME", "GYM", "HOME")[index],
            fingerprint=str(index + 2) * 64,
        )
        for index, checkin_id in enumerate(evidence_ids)
    )
    time_pattern = BehaviorPattern(
        pattern_id="time-morning",
        pattern_type=BehaviorPatternType.REPEATED_COMPLETION_TIME_OF_DAY,
        key="MORNING",
        occurrence_count=2,
        opportunity_count=3,
        ratio=Decimal("0.6667"),
        evidence_ids=_sorted_ids(evidence_ids[0], evidence_ids[2]),
    )
    location_pattern = BehaviorPattern(
        pattern_id="location-home",
        pattern_type=BehaviorPatternType.REPEATED_COMPLETION_LOCATION,
        key="HOME",
        occurrence_count=2,
        opportunity_count=3,
        ratio=Decimal("0.6667"),
        evidence_ids=_sorted_ids(evidence_ids[0], evidence_ids[2]),
    )
    skip_pattern = BehaviorPattern(
        pattern_id="skip-weekday",
        pattern_type=BehaviorPatternType.REPEATED_SKIP_WEEKDAY,
        key="WEDNESDAY",
        occurrence_count=1,
        opportunity_count=3,
        ratio=Decimal("0.3333"),
        evidence_ids=(evidence_ids[2],),
    )
    summary = BehaviorSummary(
        id=uuid4(),
        user_id=user_id,
        window_start_utc=NOW - timedelta(days=28),
        window_end_utc=NOW,
        timezone="Asia/Shanghai",
        scheduled_session_count=4,
        checked_in_session_count=3,
        completed_count=1,
        partially_completed_count=1,
        skipped_count=1,
        missing_checkin_count=1,
        completion_rate=Decimal("0.2500"),
        participation_rate=Decimal("0.5000"),
        rpe_sample_count=2,
        average_reported_rpe=Decimal("7.5000"),
        high_reported_rpe_count=1,
        repeated_time_patterns=(time_pattern,),
        repeated_location_patterns=(location_pattern,),
        repeated_skip_patterns=(skip_pattern,),
        evidence_references=evidence,
        conflict_checkin_ids=_sorted_ids(evidence_ids[0], evidence_ids[1]),
        policy_version="behavior-summary-policy-v1",
        fingerprint="a" * 64,
        created_at=NOW,
    )
    mutable = _sorted_ids(uuid4(), uuid4())
    immutable = _sorted_ids(uuid4(), uuid4())
    preserved = _sorted_ids(immutable[0], uuid4())
    calendar_bound = _sorted_ids(mutable[0], immutable[0])
    impact = RecoveryChangeImpactSnapshot(
        id=uuid4(),
        user_id=user_id,
        root_plan_id=root_plan_id,
        source_revision=3,
        source_plan_version=7,
        mutable_session_ids=mutable,
        immutable_session_ids=immutable,
        preserved_session_ids=preserved,
        calendar_bound_session_ids=calendar_bound,
        completed_checkin_ids=_sorted_ids(evidence_ids[0], evidence_ids[1]),
        weekly_frequency_before=4,
        minimum_allowed_frequency=2,
        maximum_allowed_frequency=5,
        requires_session_redesign=True,
        requires_schedule_redraft=True,
        requires_calendar_reconciliation=True,
        requires_new_plan_revision=True,
        fingerprint="b" * 64,
        created_at=NOW + timedelta(microseconds=1),
    )
    candidates = (
        RecoveryActionCandidate(
            id=uuid4(),
            action_type=RecoveryActionType.KEEP_CURRENT_PLAN,
            target_session_id=None,
            target_week_start=None,
            redesign_goal=None,
            evidence_pattern_ids=(),
            impact_snapshot_id=impact.id,
            requires_schedule_draft=False,
            requires_session_design_draft=False,
            requires_plan_revision=False,
            requires_calendar_reconciliation=False,
            deterministic_rank=0,
        ),
        RecoveryActionCandidate(
            id=uuid4(),
            action_type=RecoveryActionType.REQUEST_SESSION_RESCHEDULE,
            target_session_id=mutable[0],
            target_week_start=None,
            redesign_goal=None,
            evidence_pattern_ids=(time_pattern.pattern_id,),
            impact_snapshot_id=impact.id,
            requires_schedule_draft=True,
            requires_session_design_draft=False,
            requires_plan_revision=True,
            requires_calendar_reconciliation=True,
            deterministic_rank=1,
        ),
        RecoveryActionCandidate(
            id=uuid4(),
            action_type=RecoveryActionType.REQUEST_SESSION_REDESIGN,
            target_session_id=mutable[1],
            target_week_start=None,
            redesign_goal=RecoveryRedesignGoal.LOWER_LOAD,
            evidence_pattern_ids=(
                location_pattern.pattern_id,
                skip_pattern.pattern_id,
            ),
            impact_snapshot_id=impact.id,
            requires_schedule_draft=False,
            requires_session_design_draft=True,
            requires_plan_revision=True,
            requires_calendar_reconciliation=False,
            deterministic_rank=2,
        ),
        RecoveryActionCandidate(
            id=uuid4(),
            action_type=RecoveryActionType.NEXT_WEEK_FREQUENCY_REVIEW,
            target_session_id=None,
            target_week_start=date(2026, 8, 3),
            redesign_goal=None,
            evidence_pattern_ids=(skip_pattern.pattern_id,),
            impact_snapshot_id=impact.id,
            requires_schedule_draft=True,
            requires_session_design_draft=False,
            requires_plan_revision=True,
            requires_calendar_reconciliation=True,
            deterministic_rank=3,
        ),
    )
    candidate_set = RecoveryActionCandidateSet(
        id=uuid4(),
        user_id=user_id,
        root_plan_id=root_plan_id,
        source_revision=3,
        source_plan_version=7,
        behavior_summary_id=summary.id,
        behavior_summary_fingerprint=summary.fingerprint,
        context_snapshot_reference_id=context_id,
        context_fingerprint="c" * 64,
        change_impact_snapshot_id=impact.id,
        candidates=candidates,
        fingerprint="d" * 64,
        policy_version="recovery-candidate-policy-v1",
        prompt_version="recovery-agent-v1",
        created_at=NOW + timedelta(microseconds=2),
    )
    proposals = (
        BehaviorMemoryProposal(
            id=uuid4(),
            user_id=user_id,
            memory_type=MemoryType.PREFERRED_TIME_OF_DAY,
            proposed_key="preferred_time_of_day",
            proposed_value="MORNING",
            behavior_pattern_ids=(time_pattern.pattern_id,),
            evidence_checkin_ids=time_pattern.evidence_ids,
            confidence_tier=BehaviorConfidenceTier.STRONG,
            status=BehaviorMemoryProposalStatus.PROPOSED,
            created_at=NOW + timedelta(microseconds=3),
            expires_at=NOW + timedelta(days=14, microseconds=3),
        ),
        BehaviorMemoryProposal(
            id=uuid4(),
            user_id=user_id,
            memory_type=MemoryType.PREFERRED_LOCATION,
            proposed_key="preferred_location",
            proposed_value="HOME",
            behavior_pattern_ids=(location_pattern.pattern_id,),
            evidence_checkin_ids=location_pattern.evidence_ids,
            confidence_tier=BehaviorConfidenceTier.THRESHOLD,
            status=BehaviorMemoryProposalStatus.PROPOSED,
            created_at=NOW + timedelta(microseconds=4),
            expires_at=NOW + timedelta(days=14, microseconds=4),
        ),
    )
    draft = RecoveryDraft(
        id=uuid4(),
        request_id=request_id,
        client_request_id=client_request_id or f"recovery-{uuid4().hex}",
        user_id=user_id,
        request_payload_fingerprint=request_payload_fingerprint,
        request_fingerprint="e" * 64,
        root_plan_id=root_plan_id,
        source_revision=3,
        source_plan_version=7,
        behavior_summary_id=summary.id,
        context_snapshot_reference_id=context_id,
        change_impact_snapshot_id=impact.id,
        candidate_set_id=candidate_set.id,
        selected_action_candidate_ids=(candidates[1].id, candidates[2].id),
        unresolved_session_ids=(immutable[1],),
        behavior_memory_proposal_ids=tuple(item.id for item in proposals),
        explanation_summary="Controlled Recovery actions require review.",
        outcome=RecoveryDraftOutcome.COMPLETE,
        source=RecoveryDraftSource.MODEL,
        fallback_used=False,
        scope_status=RecoveryScopeStatus.SUPPORTED,
        status=RecoveryDraftStatus.PENDING_REVIEW,
        created_at=NOW + timedelta(microseconds=5),
        expires_at=NOW + timedelta(minutes=30, microseconds=5),
        version=1,
    )
    trace = RecoveryTrace(
        id=uuid4(),
        user_id=user_id,
        draft_id=draft.id,
        request_id=request_id,
        behavior_summary_id=summary.id,
        behavior_summary_fingerprint=summary.fingerprint,
        context_snapshot_reference_id=context_id,
        change_impact_snapshot_id=impact.id,
        candidate_set_id=candidate_set.id,
        candidate_set_fingerprint=candidate_set.fingerprint,
        scope_status=draft.scope_status,
        provider_name="controlled-provider",
        provider_version="v1",
        attempt_no=1,
        outcome=draft.outcome,
        fallback_used=draft.fallback_used,
        validation_error_code=None,
        latency_ms=12.75,
        created_at=NOW + timedelta(microseconds=6),
    )
    return RecoveryBundle(
        draft=draft,
        summary=summary,
        impact=impact,
        candidate_set=candidate_set,
        proposals=proposals,
        trace=trace,
    )


def _distinct_request_bundle(
    canonical: RecoveryBundle,
    *,
    client_request_id: str,
    offset_seconds: int = 1,
) -> RecoveryBundle:
    offset = timedelta(seconds=offset_seconds)
    request_id = uuid4()
    draft_id = uuid4()
    proposals = tuple(
        replace(
            item,
            id=uuid5(
                NAMESPACE_URL,
                f"test-recovery-proposal:{client_request_id}:{item.id}",
            ),
            created_at=item.created_at + offset,
            expires_at=item.expires_at + offset,
        )
        for item in canonical.proposals
    )
    draft = replace(
        canonical.draft,
        id=draft_id,
        request_id=request_id,
        client_request_id=client_request_id,
        request_payload_fingerprint=hashlib.sha256(
            f"payload:{client_request_id}".encode()
        ).hexdigest(),
        request_fingerprint=hashlib.sha256(
            f"request:{client_request_id}".encode()
        ).hexdigest(),
        behavior_memory_proposal_ids=tuple(item.id for item in proposals),
        created_at=canonical.draft.created_at + offset,
        expires_at=canonical.draft.expires_at + offset,
    )
    return RecoveryBundle(
        draft=draft,
        summary=replace(
            canonical.summary, created_at=canonical.summary.created_at + offset
        ),
        impact=replace(
            canonical.impact, created_at=canonical.impact.created_at + offset
        ),
        candidate_set=replace(
            canonical.candidate_set,
            created_at=canonical.candidate_set.created_at + offset,
        ),
        proposals=proposals,
        trace=replace(
            canonical.trace,
            id=uuid4(),
            draft_id=draft_id,
            request_id=request_id,
            created_at=canonical.trace.created_at + offset,
        ),
    )


class _FirstTwoSummaryReads:
    def __init__(self) -> None:
        self.count = 0
        self._lock = asyncio.Lock()
        self._release = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self.count += 1
            should_wait = self.count <= 2
            if self.count == 2:
                self._release.set()
        if should_wait:
            await self._release.wait()


class _GatedRecoverySession(AsyncSession):
    async def get(
        self,
        entity: Any,
        ident: Any,
        **kwargs: Any,
    ) -> Any:
        result = await super().get(entity, ident, **kwargs)
        gate = self.info.get("recovery_summary_gate")
        if (
            entity is RecoveryBehaviorSummaryModel
            and result is None
            and isinstance(gate, _FirstTwoSummaryReads)
        ):
            await gate.wait()
        return result


def _gated_repository(
    database: Database,
) -> tuple[MySQLRecoveryDraftRepository, _FirstTwoSummaryReads]:
    gate = _FirstTwoSummaryReads()
    sessions = async_sessionmaker(
        bind=database.engine,
        class_=_GatedRecoverySession,
        expire_on_commit=False,
        info={"recovery_summary_gate": gate},
    )
    return MySQLRecoveryDraftRepository(sessions), gate


async def _save(
    repository: MySQLRecoveryDraftRepository, bundle: RecoveryBundle
) -> RecoveryDraft:
    return await repository.save_bundle(
        draft=bundle.draft,
        behavior_summary=bundle.summary,
        impact=bundle.impact,
        candidate_set=bundle.candidate_set,
        proposals=bundle.proposals,
        trace=bundle.trace,
    )


async def _read_bundle(
    repository: MySQLRecoveryDraftRepository, user_id: UUID, draft_id: UUID
) -> tuple[object, ...]:
    return (
        await repository.get_draft(user_id, draft_id),
        await repository.get_summary(user_id, draft_id),
        await repository.get_impact(user_id, draft_id),
        await repository.get_candidate_set(user_id, draft_id),
        await repository.list_proposals(user_id, draft_id),
        await repository.get_trace(user_id, draft_id),
    )


async def _bundle_counts(database: Database, bundle: RecoveryBundle) -> dict[str, int]:
    queries = {
        "summary": (
            "SELECT COUNT(*) FROM recovery_behavior_summary WHERE id = :value",
            str(bundle.summary.id),
        ),
        "impact": (
            "SELECT COUNT(*) FROM recovery_change_impact WHERE id = :value",
            str(bundle.impact.id),
        ),
        "candidate_set": (
            "SELECT COUNT(*) FROM recovery_candidate_set WHERE id = :value",
            str(bundle.candidate_set.id),
        ),
        "candidates": (
            "SELECT COUNT(*) FROM recovery_action_candidate "
            "WHERE candidate_set_id = :value",
            str(bundle.candidate_set.id),
        ),
        "draft": (
            "SELECT COUNT(*) FROM recovery_draft WHERE id = :value",
            str(bundle.draft.id),
        ),
        "proposals": (
            "SELECT COUNT(*) FROM recovery_memory_proposal WHERE draft_id = :value",
            str(bundle.draft.id),
        ),
        "trace": (
            "SELECT COUNT(*) FROM recovery_trace WHERE draft_id = :value",
            str(bundle.draft.id),
        ),
    }
    async with database.session_factory() as session:
        return {
            name: int(await session.scalar(text(statement), {"value": value}) or 0)
            for name, (statement, value) in queries.items()
        }


async def _owner_recovery_counts(
    database: Database,
    user_id: UUID,
) -> dict[str, int]:
    table_names = {
        "summary": "recovery_behavior_summary",
        "impact": "recovery_change_impact",
        "candidate_set": "recovery_candidate_set",
        "candidates": "recovery_action_candidate",
        "draft": "recovery_draft",
        "proposals": "recovery_memory_proposal",
        "trace": "recovery_trace",
    }
    async with database.session_factory() as session:
        return {
            name: int(
                await session.scalar(
                    text(f"SELECT COUNT(*) FROM {table} WHERE user_id = :user_id"),
                    {"user_id": str(user_id)},
                )
                or 0
            )
            for name, table in table_names.items()
        }


async def _cleanup(database: Database, user_ids: tuple[UUID, ...]) -> None:
    if not user_ids:
        return
    values = [str(value) for value in user_ids]
    parameters = {f"user_{index}": value for index, value in enumerate(values)}
    placeholders = ", ".join(f":user_{index}" for index in range(len(values)))
    async with database.session_factory() as session:
        async with session.begin():
            for table in RECOVERY_TABLES:
                await session.execute(
                    text(f"DELETE FROM {table} WHERE user_id IN ({placeholders})"),
                    parameters,
                )
            await session.execute(
                delete(UserAccountModel).where(UserAccountModel.id.in_(values))
            )


def _run_alembic(
    database_url: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def _run_offline(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments, "--sql"],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_recovery_bundle_reuses_validated_artifacts_across_distinct_requests(  # noqa: E501
    mysql_test_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated_users = []
    users = MySQLUserAccountRepository(mysql_test_database.session_factory)
    original_flush = AsyncSession.flush

    async def create_user(label: str):
        user = replace(
            make_user(),
            email=f"recovery-reuse-{label}-{uuid4().hex}@fitweek.test",
        )
        await users.save(user)
        generated_users.append(user)
        return user

    try:
        sequential_user = await create_user("sequential")
        sequential_first = _bundle(
            sequential_user.id,
            client_request_id=f"sequential-a-{uuid4().hex}",
        )
        sequential_second = _distinct_request_bundle(
            sequential_first,
            client_request_id=f"sequential-b-{uuid4().hex}",
        )
        sequential_repository = _repository(mysql_test_database)
        assert await _save(sequential_repository, sequential_first) == (
            sequential_first.draft
        )
        assert await _save(sequential_repository, sequential_second) == (
            sequential_second.draft
        )
        first_read = await _read_bundle(
            sequential_repository,
            sequential_user.id,
            sequential_first.draft.id,
        )
        second_read = await _read_bundle(
            sequential_repository,
            sequential_user.id,
            sequential_second.draft.id,
        )
        assert first_read == (
            sequential_first.draft,
            sequential_first.summary,
            sequential_first.impact,
            sequential_first.candidate_set,
            sequential_first.proposals,
            sequential_first.trace,
        )
        assert second_read == (
            sequential_second.draft,
            sequential_first.summary,
            sequential_first.impact,
            sequential_first.candidate_set,
            sequential_second.proposals,
            sequential_second.trace,
        )
        assert set(sequential_first.draft.behavior_memory_proposal_ids).isdisjoint(
            sequential_second.draft.behavior_memory_proposal_ids
        )
        expected_two_bundle_counts = {
            "summary": 1,
            "impact": 1,
            "candidate_set": 1,
            "candidates": len(sequential_first.candidate_set.candidates),
            "draft": 2,
            "proposals": len(sequential_first.proposals)
            + len(sequential_second.proposals),
            "trace": 2,
        }
        assert (
            await _owner_recovery_counts(
                mysql_test_database,
                sequential_user.id,
            )
            == expected_two_bundle_counts
        )

        for round_index in range(5):
            race_user = await create_user(f"race-{round_index}")
            race_first = _bundle(
                race_user.id,
                client_request_id=f"race-{round_index}-a-{uuid4().hex}",
            )
            race_second = _distinct_request_bundle(
                race_first,
                client_request_id=f"race-{round_index}-b-{uuid4().hex}",
            )
            race_repository, gate = _gated_repository(mysql_test_database)
            race_results = await asyncio.gather(
                _save(race_repository, race_first),
                _save(race_repository, race_second),
                return_exceptions=True,
            )
            assert gate.count == 2
            assert all(isinstance(item, RecoveryDraft) for item in race_results)
            assert {
                item.id for item in race_results if isinstance(item, RecoveryDraft)
            } == {race_first.draft.id, race_second.draft.id}
            assert (
                await _owner_recovery_counts(mysql_test_database, race_user.id)
                == expected_two_bundle_counts
            )

        mismatch_user = await create_user("mismatch")
        mismatch_first = _bundle(
            mismatch_user.id,
            client_request_id=f"mismatch-a-{uuid4().hex}",
        )
        mismatch_repository = _repository(mysql_test_database)
        await _save(mismatch_repository, mismatch_first)
        mismatch_before = await _owner_recovery_counts(
            mysql_test_database,
            mismatch_user.id,
        )
        mismatch_frozen = await _read_bundle(
            mismatch_repository,
            mismatch_user.id,
            mismatch_first.draft.id,
        )
        mismatch_second = _distinct_request_bundle(
            mismatch_first,
            client_request_id=f"mismatch-b-{uuid4().hex}",
        )
        changed_candidates = (
            replace(
                mismatch_second.candidate_set.candidates[0],
                requires_schedule_draft=True,
            ),
            *mismatch_second.candidate_set.candidates[1:],
        )
        mismatch_second = replace(
            mismatch_second,
            candidate_set=replace(
                mismatch_second.candidate_set,
                candidates=changed_candidates,
            ),
        )
        with pytest.raises(RepositoryConflictError):
            await _save(mismatch_repository, mismatch_second)
        assert (
            await _owner_recovery_counts(mysql_test_database, mismatch_user.id)
            == mismatch_before
        )
        assert (
            await _read_bundle(
                mismatch_repository,
                mismatch_user.id,
                mismatch_first.draft.id,
            )
            == mismatch_frozen
        )

        rollback_user = await create_user("rollback")
        rollback_first = _bundle(
            rollback_user.id,
            client_request_id=f"rollback-a-{uuid4().hex}",
        )
        rollback_second = _distinct_request_bundle(
            rollback_first,
            client_request_id=f"rollback-b-{uuid4().hex}",
        )
        rollback_repository = _repository(mysql_test_database)
        await _save(rollback_repository, rollback_first)
        rollback_before = await _owner_recovery_counts(
            mysql_test_database,
            rollback_user.id,
        )

        async def fail_after_reused_canonical(
            self: AsyncSession,
            *args: object,
            **kwargs: object,
        ) -> None:
            has_new_draft = any(
                isinstance(value, RecoveryDraftModel) for value in self.new
            )
            await original_flush(self, *args, **kwargs)
            if has_new_draft:
                raise RuntimeError("controlled post-reuse rollback")

        monkeypatch.setattr(
            AsyncSession,
            "flush",
            fail_after_reused_canonical,
        )
        with pytest.raises(RuntimeError, match="controlled post-reuse rollback"):
            await _save(rollback_repository, rollback_second)
        monkeypatch.setattr(AsyncSession, "flush", original_flush)
        assert (
            await _owner_recovery_counts(mysql_test_database, rollback_user.id)
            == rollback_before
        )
        assert (
            await rollback_repository.get_draft(
                rollback_user.id,
                rollback_second.draft.id,
            )
            is None
        )

        missing_user_bundle = _bundle(uuid4())
        captured_errors: list[IntegrityError] = []

        def capture_integrity(exception_context: Any) -> None:
            error = exception_context.sqlalchemy_exception
            if isinstance(error, IntegrityError):
                captured_errors.append(error)

        event.listen(
            mysql_test_database.engine.sync_engine,
            "handle_error",
            capture_integrity,
        )
        try:
            with pytest.raises(IntegrityError) as unrelated:
                await _save(_repository(mysql_test_database), missing_user_bundle)
        finally:
            event.remove(
                mysql_test_database.engine.sync_engine,
                "handle_error",
                capture_integrity,
            )
        assert captured_errors
        assert unrelated.value is captured_errors[-1]
        assert all(
            value == 0
            for value in (
                await _bundle_counts(mysql_test_database, missing_user_bundle)
            ).values()
        )
    finally:
        monkeypatch.setattr(AsyncSession, "flush", original_flush)
        await _cleanup(
            mysql_test_database,
            tuple(user.id for user in generated_users),
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_recovery_bundle_round_trips_every_artifact_after_repository_restart(  # noqa: E501
    mysql_test_database: Database,
) -> None:
    user = replace(make_user(), email=f"recovery-roundtrip-{uuid4().hex}@fitweek.test")
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
    bundle = _bundle(user.id)
    first = _repository(mysql_test_database)
    second = _repository(mysql_test_database)
    try:
        stored = await _save(first, bundle)
        assert stored == bundle.draft
        assert await _read_bundle(second, user.id, bundle.draft.id) == (
            bundle.draft,
            bundle.summary,
            bundle.impact,
            bundle.candidate_set,
            bundle.proposals,
            bundle.trace,
        )
        assert (
            await second.get_by_request(user.id, bundle.draft.client_request_id)
            == bundle.draft
        )
        assert await _bundle_counts(mysql_test_database, bundle) == {
            "summary": 1,
            "impact": 1,
            "candidate_set": 1,
            "candidates": len(bundle.candidate_set.candidates),
            "draft": 1,
            "proposals": len(bundle.proposals),
            "trace": 1,
        }
    finally:
        await _cleanup(mysql_test_database, (user.id,))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_recovery_bundle_is_atomic_unique_and_user_isolated(
    mysql_test_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = replace(make_user(), email=f"recovery-owner-{uuid4().hex}@fitweek.test")
    other = replace(make_user(), email=f"recovery-other-{uuid4().hex}@fitweek.test")
    rollback_user = replace(
        make_user(), email=f"recovery-rollback-{uuid4().hex}@fitweek.test"
    )
    users = MySQLUserAccountRepository(mysql_test_database.session_factory)
    for user in (owner, other, rollback_user):
        await users.save(user)
    request_key = f"recovery-race-{uuid4().hex}"
    first_bundle = _bundle(
        owner.id,
        client_request_id=request_key,
        request_payload_fingerprint="1" * 64,
    )
    second_bundle = _bundle(
        owner.id,
        client_request_id=request_key,
        request_payload_fingerprint="2" * 64,
    )
    first_repository = _repository(mysql_test_database)
    second_repository = _repository(mysql_test_database)
    original_flush = AsyncSession.flush
    try:
        wrong_owner = replace(first_bundle.summary, user_id=other.id)
        with pytest.raises(RepositoryConflictError):
            await _save(
                first_repository,
                replace(first_bundle, summary=wrong_owner),
            )
        wrong_link = replace(first_bundle.candidate_set, behavior_summary_id=uuid4())
        with pytest.raises(RepositoryConflictError):
            await _save(
                first_repository,
                replace(first_bundle, candidate_set=wrong_link),
            )
        wrong_proposals = replace(
            first_bundle.draft,
            behavior_memory_proposal_ids=(uuid4(),),
        )
        with pytest.raises(RepositoryConflictError):
            await _save(
                first_repository,
                replace(first_bundle, draft=wrong_proposals),
            )

        results = await asyncio.gather(
            _save(first_repository, first_bundle),
            _save(second_repository, second_bundle),
            return_exceptions=True,
        )
        assert sum(isinstance(value, RecoveryDraft) for value in results) == 1
        assert sum(isinstance(value, RepositoryUniqueError) for value in results) == 1
        winner = await first_repository.get_by_request(owner.id, request_key)
        assert winner is not None
        winner_bundle = (
            first_bundle if winner.id == first_bundle.draft.id else second_bundle
        )
        loser_bundle = second_bundle if winner_bundle is first_bundle else first_bundle
        assert await _read_bundle(second_repository, owner.id, winner.id) == (
            winner_bundle.draft,
            winner_bundle.summary,
            winner_bundle.impact,
            winner_bundle.candidate_set,
            winner_bundle.proposals,
            winner_bundle.trace,
        )
        assert all(
            value == 0
            for value in (
                await _bundle_counts(mysql_test_database, loser_bundle)
            ).values()
        )

        same_request_other_user = _bundle(other.id, client_request_id=request_key)
        assert await _save(second_repository, same_request_other_user) == (
            same_request_other_user.draft
        )

        duplicate_id_bundle = _bundle(
            owner.id, client_request_id=f"duplicate-id-{uuid4().hex}"
        )
        duplicate_draft = replace(duplicate_id_bundle.draft, id=winner.id)
        duplicate_trace = replace(duplicate_id_bundle.trace, draft_id=winner.id)
        with pytest.raises(RepositoryUniqueError):
            await _save(
                first_repository,
                replace(
                    duplicate_id_bundle,
                    draft=duplicate_draft,
                    trace=duplicate_trace,
                ),
            )

        missing_user_bundle = _bundle(uuid4())
        with pytest.raises(IntegrityError):
            await _save(first_repository, missing_user_bundle)
        assert all(
            value == 0
            for value in (
                await _bundle_counts(mysql_test_database, missing_user_bundle)
            ).values()
        )

        rollback_bundle = _bundle(rollback_user.id)

        async def fail_after_recovery_flush(
            self: AsyncSession, *args: object, **kwargs: object
        ) -> None:
            has_recovery_rows = any(
                type(value).__name__.startswith("Recovery") for value in self.new
            )
            await original_flush(self, *args, **kwargs)
            if has_recovery_rows:
                raise RuntimeError("controlled recovery bundle rollback")

        monkeypatch.setattr(AsyncSession, "flush", fail_after_recovery_flush)
        with pytest.raises(RuntimeError, match="controlled recovery bundle rollback"):
            await _save(first_repository, rollback_bundle)
        monkeypatch.setattr(AsyncSession, "flush", original_flush)
        assert all(
            value == 0
            for value in (
                await _bundle_counts(mysql_test_database, rollback_bundle)
            ).values()
        )

        assert await first_repository.get_draft(other.id, winner.id) is None
        assert await first_repository.get_summary(other.id, winner.id) is None
        assert await first_repository.get_impact(other.id, winner.id) is None
        assert await first_repository.get_candidate_set(other.id, winner.id) is None
        assert await first_repository.get_trace(other.id, winner.id) is None
        assert await first_repository.list_proposals(other.id, winner.id) == ()
        assert (
            await first_repository.get_by_request(other.id, winner.client_request_id)
            == same_request_other_user.draft
        )
    finally:
        monkeypatch.setattr(AsyncSession, "flush", original_flush)
        await _cleanup(mysql_test_database, (owner.id, other.id, rollback_user.id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_recovery_review_compare_and_swap_freezes_artifacts(
    mysql_test_database: Database,
) -> None:
    user = replace(make_user(), email=f"recovery-cas-{uuid4().hex}@fitweek.test")
    await MySQLUserAccountRepository(mysql_test_database.session_factory).save(user)
    bundle = _bundle(user.id)
    first = _repository(mysql_test_database)
    second = _repository(mysql_test_database)
    try:
        pending = await _save(first, bundle)
        frozen_before = (await _read_bundle(first, user.id, pending.id))[1:]
        accepted_left = pending.accept(
            expected_version=1, at=NOW + timedelta(minutes=1)
        )
        accepted_right = pending.accept(
            expected_version=1, at=NOW + timedelta(minutes=1)
        )
        results = await asyncio.gather(
            first.update_draft(accepted_left),
            second.update_draft(accepted_right),
            return_exceptions=True,
        )
        assert sum(isinstance(value, RecoveryDraft) for value in results) == 1
        assert sum(isinstance(value, RepositoryConflictError) for value in results) == 1
        accepted = await second.get_draft(user.id, pending.id)
        assert accepted is not None
        assert accepted.status is RecoveryDraftStatus.ACCEPTED
        assert accepted.version == 2
        assert accepted.reviewed_at == NOW + timedelta(minutes=1)

        stale_reject = pending.reject(expected_version=1, at=NOW + timedelta(minutes=2))
        with pytest.raises(RepositoryConflictError):
            await first.update_draft(stale_reject)
        immutable_mutation = replace(accepted, request_fingerprint="9" * 64, version=3)
        with pytest.raises(RepositoryConflictError):
            await first.update_draft(immutable_mutation)

        applied = accepted.mark_applied(
            application_result_id=uuid4(),
            root_plan_id=accepted.root_plan_id,
            source_revision=accepted.source_revision,
            created_revision=accepted.source_revision + 1,
            affected_session_ids=_sorted_ids(uuid4(), uuid4()),
            session_design_draft_ids=_sorted_ids(uuid4(), uuid4()),
            schedule_draft_ids=(uuid4(),),
            at=NOW + timedelta(minutes=3),
        )
        assert await first.update_draft(applied) == applied
        durable_applied = await second.get_draft(user.id, pending.id)
        assert durable_applied == applied
        assert durable_applied.status is RecoveryDraftStatus.APPLIED
        assert durable_applied.version == 3
        assert durable_applied.application_result_id == applied.application_result_id
        assert durable_applied.applied_created_revision == 4
        assert (await _read_bundle(second, user.id, pending.id))[1:] == frozen_before
    finally:
        await _cleanup(mysql_test_database, (user.id,))


@pytest.mark.integration
def test_mysql_recovery_migration_round_trip_preserves_0010_schema(
    mysql_test_database: Database, mysql_test_url: str
) -> None:
    del mysql_test_database
    migration = Path("migrations/versions/0011_recovery_draft_persistence.py")
    assert migration.exists(), "Recovery persistence migration is missing"
    upgrade = _run_offline(
        "upgrade",
        "0010_calendar_operation_persistence:0011_recovery_draft_persistence",
    )
    assert upgrade.returncode == 0, upgrade.stderr
    for table in RECOVERY_TABLES:
        assert f"CREATE TABLE {table}" in upgrade.stdout
    downgrade = _run_offline(
        "downgrade",
        "0011_recovery_draft_persistence:0010_calendar_operation_persistence",
    )
    assert downgrade.returncode == 0, downgrade.stderr
    for table in reversed(RECOVERY_TABLES):
        assert f"DROP TABLE {table}" in downgrade.stdout

    try:
        _run_alembic(mysql_test_url, "downgrade", "0010_calendar_operation_persistence")
        before = _run_alembic(mysql_test_url, "current").stdout
        assert "0010_calendar_operation_persistence" in before
        _run_alembic(mysql_test_url, "upgrade", "0011_recovery_draft_persistence")
        assert (
            "0011_recovery_draft_persistence"
            in _run_alembic(mysql_test_url, "current").stdout
        )

        expected = set(RECOVERY_TABLES)

        async def live_contract() -> tuple[set[str], dict[str, str], set[str]]:
            settings = _repository_database(mysql_test_url)
            try:
                async with settings.session_factory() as session:
                    tables = {
                        str(row[0])
                        for row in (await session.execute(text("SHOW TABLES"))).all()
                    }
                    columns = {
                        str(row[0]): str(row[1]).lower()
                        for row in (
                            await session.execute(
                                text("SHOW COLUMNS FROM recovery_draft")
                            )
                        ).all()
                    }
                    indexes = {
                        str(row[2])
                        for row in (
                            await session.execute(
                                text("SHOW INDEX FROM recovery_draft")
                            )
                        ).all()
                    }
                    return tables, columns, indexes
            finally:
                await settings.dispose()

        tables, draft_columns, draft_indexes = asyncio.run(live_contract())
        assert expected <= tables
        assert {
            "calendar_event_binding",
            "calendar_operation_draft",
            "calendar_operation_item",
            "calendar_operation_attempt",
        } <= tables
        assert draft_columns["client_request_id"] == "varchar(128)"
        assert draft_columns["created_at"] == "datetime(6)"
        assert {
            "uq_recovery_draft_user_request",
            "ix_recovery_draft_user_status",
            "ix_recovery_draft_user_plan",
        } <= draft_indexes
        for table in RECOVERY_TABLES:
            assert "user_id" in Base.metadata.tables[table].columns
        assert (
            Base.metadata.tables["recovery_draft"].c.client_request_id.type.length
            == 128
        )
        assert Base.metadata.tables["recovery_trace"].c.draft_id.unique is True

        _run_alembic(mysql_test_url, "downgrade", "0010_calendar_operation_persistence")

        async def downgraded_tables() -> set[str]:
            database = _repository_database(mysql_test_url)
            try:
                async with database.session_factory() as session:
                    return {
                        str(row[0])
                        for row in (await session.execute(text("SHOW TABLES"))).all()
                    }
            finally:
                await database.dispose()

        after_downgrade = asyncio.run(downgraded_tables())
        assert not expected & after_downgrade
        assert {
            "calendar_event_binding",
            "calendar_operation_draft",
            "calendar_operation_item",
            "calendar_operation_attempt",
        } <= after_downgrade
    finally:
        _run_alembic(mysql_test_url, "upgrade", "head")
    assert (
        "0012_recovery_application_persistence"
        in _run_alembic(mysql_test_url, "heads").stdout
    )
    assert (
        "0012_recovery_application_persistence"
        in _run_alembic(mysql_test_url, "current").stdout
    )


def _repository_database(database_url: str) -> Database:
    from pydantic import SecretStr

    from app.config import Settings

    return Database(
        Settings(
            app_env="test",
            database_url=SecretStr(database_url),
            redis_enabled=False,
            _env_file=None,
        )
    )
