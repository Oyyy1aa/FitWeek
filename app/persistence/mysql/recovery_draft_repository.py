"""MySQL persistence for immutable Recovery Draft artifact bundles."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, NoReturn, cast
from uuid import UUID

from sqlalchemy import select
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
from app.persistence.mysql.models import (
    RecoveryActionCandidateModel,
    RecoveryBehaviorSummaryModel,
    RecoveryCandidateSetModel,
    RecoveryChangeImpactModel,
    RecoveryDraftModel,
    RecoveryMemoryProposalModel,
    RecoveryTraceModel,
)


class _CanonicalArtifactInsertError(Exception):
    def __init__(self, original: IntegrityError) -> None:
        self.original = original
        super().__init__("canonical Recovery artifact insert failed")


class MySQLRecoveryDraftRepository:
    """Store one Recovery bundle atomically and review its Draft with CAS."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_draft(self, user_id: UUID, draft_id: UUID) -> RecoveryDraft | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(RecoveryDraftModel).where(
                    RecoveryDraftModel.id == str(draft_id),
                    RecoveryDraftModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._draft_from_row(row)

    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> RecoveryDraft | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(RecoveryDraftModel).where(
                    RecoveryDraftModel.user_id == str(user_id),
                    RecoveryDraftModel.client_request_id == client_request_id,
                )
            )
            return None if row is None else self._draft_from_row(row)

    async def save_bundle(
        self,
        *,
        draft: RecoveryDraft,
        behavior_summary: BehaviorSummary,
        impact: RecoveryChangeImpactSnapshot,
        candidate_set: RecoveryActionCandidateSet,
        proposals: tuple[BehaviorMemoryProposal, ...],
        trace: RecoveryTrace,
    ) -> RecoveryDraft:
        self._validate_bundle(
            draft=draft,
            behavior_summary=behavior_summary,
            impact=impact,
            candidate_set=candidate_set,
            proposals=proposals,
            trace=trace,
        )
        try:
            await self._save_bundle_once(
                draft=draft,
                behavior_summary=behavior_summary,
                impact=impact,
                candidate_set=candidate_set,
                proposals=proposals,
                trace=trace,
            )
        except _CanonicalArtifactInsertError as collision:
            if not await self._canonical_artifacts_match(
                user_id=draft.user_id,
                behavior_summary=behavior_summary,
                impact=impact,
                candidate_set=candidate_set,
            ):
                raise collision.original from None
            try:
                await self._save_bundle_once(
                    draft=draft,
                    behavior_summary=behavior_summary,
                    impact=impact,
                    candidate_set=candidate_set,
                    proposals=proposals,
                    trace=trace,
                )
            except _CanonicalArtifactInsertError as repeated:
                raise repeated.original from None
            except IntegrityError as exc:
                await self._raise_integrity(draft, exc)
        except IntegrityError as exc:
            await self._raise_integrity(draft, exc)
        restored = await self.get_draft(draft.user_id, draft.id)
        if restored is None:
            raise RuntimeError("Committed Recovery Draft could not be reconstructed.")
        return restored

    async def _save_bundle_once(
        self,
        *,
        draft: RecoveryDraft,
        behavior_summary: BehaviorSummary,
        impact: RecoveryChangeImpactSnapshot,
        candidate_set: RecoveryActionCandidateSet,
        proposals: tuple[BehaviorMemoryProposal, ...],
        trace: RecoveryTrace,
    ) -> None:
        async with self._sessions() as session:
            async with session.begin():
                await self._insert_or_validate_canonical(
                    session,
                    user_id=draft.user_id,
                    behavior_summary=behavior_summary,
                    impact=impact,
                    candidate_set=candidate_set,
                )
                session.add(self._draft_row(draft))
                await session.flush()
                session.add_all(self._proposal_rows(draft.id, proposals))
                session.add(self._trace_row(trace))
                await session.flush()

    async def _insert_or_validate_canonical(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        behavior_summary: BehaviorSummary,
        impact: RecoveryChangeImpactSnapshot,
        candidate_set: RecoveryActionCandidateSet,
    ) -> None:
        summary_row = await session.get(
            RecoveryBehaviorSummaryModel,
            str(behavior_summary.id),
        )
        impact_row = await session.get(
            RecoveryChangeImpactModel,
            str(impact.id),
        )
        inserted_root = False
        if summary_row is None:
            session.add(self._summary_row(behavior_summary))
            inserted_root = True
        else:
            self._validate_summary_row(summary_row, behavior_summary)
        if impact_row is None:
            session.add(self._impact_row(impact))
            inserted_root = True
        else:
            self._validate_impact_row(impact_row, impact)
        if inserted_root:
            await self._flush_canonical(session)

        candidate_set_row = await session.get(
            RecoveryCandidateSetModel,
            str(candidate_set.id),
        )
        if candidate_set_row is None:
            session.add(self._candidate_set_row(candidate_set))
            await self._flush_canonical(session)
            session.add_all(
                self._candidate_rows(
                    user_id,
                    candidate_set.id,
                    candidate_set.candidates,
                )
            )
            await self._flush_canonical(session)
            return
        candidate_rows = await self._candidate_rows_for_set(
            session,
            candidate_set.id,
        )
        self._validate_candidate_set_rows(
            candidate_set_row,
            candidate_rows,
            candidate_set,
        )

    @staticmethod
    async def _flush_canonical(session: AsyncSession) -> None:
        try:
            await session.flush()
        except IntegrityError as exc:
            raise _CanonicalArtifactInsertError(exc) from exc

    async def _canonical_artifacts_match(
        self,
        *,
        user_id: UUID,
        behavior_summary: BehaviorSummary,
        impact: RecoveryChangeImpactSnapshot,
        candidate_set: RecoveryActionCandidateSet,
    ) -> bool:
        async with self._sessions() as session:
            summary_row = await session.get(
                RecoveryBehaviorSummaryModel,
                str(behavior_summary.id),
            )
            impact_row = await session.get(
                RecoveryChangeImpactModel,
                str(impact.id),
            )
            candidate_set_row = await session.get(
                RecoveryCandidateSetModel,
                str(candidate_set.id),
            )
            complete = True
            if summary_row is None:
                complete = False
            else:
                self._validate_summary_row(summary_row, behavior_summary)
            if impact_row is None:
                complete = False
            else:
                self._validate_impact_row(impact_row, impact)
            if candidate_set_row is None:
                complete = False
            else:
                candidate_rows = await self._candidate_rows_for_set(
                    session,
                    candidate_set.id,
                )
                self._validate_candidate_set_rows(
                    candidate_set_row,
                    candidate_rows,
                    candidate_set,
                )
            if complete and (
                behavior_summary.user_id != user_id
                or impact.user_id != user_id
                or candidate_set.user_id != user_id
            ):
                self._canonical_conflict("RecoveryCanonicalOwner", user_id)
            return complete

    @staticmethod
    async def _candidate_rows_for_set(
        session: AsyncSession,
        candidate_set_id: UUID,
    ) -> Sequence[RecoveryActionCandidateModel]:
        return (
            await session.scalars(
                select(RecoveryActionCandidateModel)
                .where(
                    RecoveryActionCandidateModel.candidate_set_id
                    == str(candidate_set_id)
                )
                .order_by(RecoveryActionCandidateModel.sequence_no)
            )
        ).all()

    @classmethod
    def _validate_summary_row(
        cls,
        row: RecoveryBehaviorSummaryModel,
        candidate: BehaviorSummary,
    ) -> None:
        durable = cls._summary_from_row(row)
        if replace(durable, created_at=candidate.created_at) != candidate:
            cls._canonical_conflict("RecoveryBehaviorSummary", candidate.id)

    @classmethod
    def _validate_impact_row(
        cls,
        row: RecoveryChangeImpactModel,
        candidate: RecoveryChangeImpactSnapshot,
    ) -> None:
        durable = cls._impact_from_row(row)
        if replace(durable, created_at=candidate.created_at) != candidate:
            cls._canonical_conflict("RecoveryChangeImpact", candidate.id)

    @classmethod
    def _validate_candidate_set_rows(
        cls,
        row: RecoveryCandidateSetModel,
        rows: Sequence[RecoveryActionCandidateModel],
        candidate: RecoveryActionCandidateSet,
    ) -> None:
        if len(rows) != len(candidate.candidates):
            cls._canonical_conflict("RecoveryCandidateSet", candidate.id)
        for sequence_no, (stored, expected) in enumerate(
            zip(rows, candidate.candidates, strict=True),
            start=1,
        ):
            if (
                stored.id != str(expected.id)
                or stored.user_id != str(candidate.user_id)
                or stored.candidate_set_id != str(candidate.id)
                or stored.sequence_no != sequence_no
                or cls._candidate_from_row(stored) != expected
            ):
                cls._canonical_conflict("RecoveryActionCandidate", expected.id)
        durable = cls._candidate_set_from_row(row, rows)
        if replace(durable, created_at=candidate.created_at) != candidate:
            cls._canonical_conflict("RecoveryCandidateSet", candidate.id)

    @staticmethod
    def _canonical_conflict(resource: str, resource_id: object) -> NoReturn:
        raise RepositoryConflictError(
            resource,
            resource_id,
            expected_version=1,
            actual_version=1,
        )

    async def _raise_integrity(
        self,
        draft: RecoveryDraft,
        exc: IntegrityError,
    ) -> NoReturn:
        if await self._draft_id_exists(draft.id):
            raise RepositoryUniqueError("recovery_draft.id", draft.id) from exc
        if (
            await self.get_by_request(draft.user_id, draft.client_request_id)
            is not None
        ):
            raise RepositoryUniqueError(
                "recovery_draft.user_request",
                (draft.user_id, draft.client_request_id),
            ) from exc
        raise exc

    async def update_draft(self, draft: RecoveryDraft) -> RecoveryDraft:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(RecoveryDraftModel)
                    .where(
                        RecoveryDraftModel.id == str(draft.id),
                        RecoveryDraftModel.user_id == str(draft.user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    raise RepositoryUniqueError("recovery_draft.id", draft.id)
                current = self._draft_from_row(row)
                expected = current.version + 1
                if draft.version != expected:
                    raise RepositoryConflictError(
                        "RecoveryDraft",
                        draft.id,
                        expected_version=expected,
                        actual_version=draft.version,
                    )
                if self._immutable_draft_fields(
                    current
                ) != self._immutable_draft_fields(draft):
                    raise RepositoryConflictError(
                        "RecoveryDraft",
                        draft.id,
                        expected_version=expected,
                        actual_version=draft.version,
                    )
                allowed = {
                    RecoveryDraftStatus.PENDING_REVIEW: {
                        RecoveryDraftStatus.ACCEPTED,
                        RecoveryDraftStatus.REJECTED,
                        RecoveryDraftStatus.EXPIRED,
                    },
                    RecoveryDraftStatus.ACCEPTED: {RecoveryDraftStatus.APPLIED},
                }
                if draft.status not in allowed.get(current.status, set()):
                    raise RepositoryConflictError(
                        "RecoveryDraft",
                        draft.id,
                        expected_version=expected,
                        actual_version=draft.version,
                    )
                self._apply_draft(row, draft)
                await session.flush()
        restored = await self.get_draft(draft.user_id, draft.id)
        if restored is None:
            raise RuntimeError("Updated Recovery Draft could not be reconstructed.")
        return restored

    async def get_summary(
        self, user_id: UUID, draft_id: UUID
    ) -> BehaviorSummary | None:
        async with self._sessions() as session:
            draft = await self._draft_row_for_user(session, user_id, draft_id)
            if draft is None:
                return None
            row = await session.get(
                RecoveryBehaviorSummaryModel, draft.behavior_summary_id
            )
            if row is None or row.user_id != str(user_id):
                return None
            return self._summary_from_row(row)

    async def get_impact(
        self, user_id: UUID, draft_id: UUID
    ) -> RecoveryChangeImpactSnapshot | None:
        async with self._sessions() as session:
            draft = await self._draft_row_for_user(session, user_id, draft_id)
            if draft is None:
                return None
            row = await session.get(
                RecoveryChangeImpactModel, draft.change_impact_snapshot_id
            )
            if row is None or row.user_id != str(user_id):
                return None
            return self._impact_from_row(row)

    async def get_candidate_set(
        self, user_id: UUID, draft_id: UUID
    ) -> RecoveryActionCandidateSet | None:
        async with self._sessions() as session:
            draft = await self._draft_row_for_user(session, user_id, draft_id)
            if draft is None:
                return None
            row = await session.get(RecoveryCandidateSetModel, draft.candidate_set_id)
            if row is None or row.user_id != str(user_id):
                return None
            candidates = (
                await session.scalars(
                    select(RecoveryActionCandidateModel)
                    .where(
                        RecoveryActionCandidateModel.candidate_set_id == row.id,
                        RecoveryActionCandidateModel.user_id == str(user_id),
                    )
                    .order_by(RecoveryActionCandidateModel.sequence_no)
                )
            ).all()
            return self._candidate_set_from_row(row, candidates)

    async def get_trace(self, user_id: UUID, draft_id: UUID) -> RecoveryTrace | None:
        async with self._sessions() as session:
            draft = await self._draft_row_for_user(session, user_id, draft_id)
            if draft is None:
                return None
            row = await session.scalar(
                select(RecoveryTraceModel).where(
                    RecoveryTraceModel.draft_id == draft.id,
                    RecoveryTraceModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._trace_from_row(row)

    async def list_proposals(
        self, user_id: UUID, draft_id: UUID
    ) -> tuple[BehaviorMemoryProposal, ...]:
        async with self._sessions() as session:
            draft = await self._draft_row_for_user(session, user_id, draft_id)
            if draft is None:
                return ()
            rows = (
                await session.scalars(
                    select(RecoveryMemoryProposalModel)
                    .where(
                        RecoveryMemoryProposalModel.draft_id == draft.id,
                        RecoveryMemoryProposalModel.user_id == str(user_id),
                    )
                    .order_by(RecoveryMemoryProposalModel.sequence_no)
                )
            ).all()
            return tuple(self._proposal_from_row(row) for row in rows)

    async def _draft_id_exists(self, draft_id: UUID) -> bool:
        async with self._sessions() as session:
            return (
                await session.scalar(
                    select(RecoveryDraftModel.id).where(
                        RecoveryDraftModel.id == str(draft_id)
                    )
                )
                is not None
            )

    @staticmethod
    async def _draft_row_for_user(
        session: AsyncSession, user_id: UUID, draft_id: UUID
    ) -> RecoveryDraftModel | None:
        return cast(
            RecoveryDraftModel | None,
            await session.scalar(
                select(RecoveryDraftModel).where(
                    RecoveryDraftModel.id == str(draft_id),
                    RecoveryDraftModel.user_id == str(user_id),
                )
            ),
        )

    @classmethod
    def _validate_bundle(
        cls,
        *,
        draft: RecoveryDraft,
        behavior_summary: BehaviorSummary,
        impact: RecoveryChangeImpactSnapshot,
        candidate_set: RecoveryActionCandidateSet,
        proposals: tuple[BehaviorMemoryProposal, ...],
        trace: RecoveryTrace,
    ) -> None:
        owners = (
            behavior_summary.user_id,
            impact.user_id,
            candidate_set.user_id,
            trace.user_id,
            *(item.user_id for item in proposals),
        )
        if any(owner != draft.user_id for owner in owners) or draft.version != 1:
            cls._bundle_conflict(draft)
        proposal_ids = tuple(item.id for item in proposals)
        candidate_ids = set(candidate_set.candidate_ids)
        if (
            draft.behavior_summary_id != behavior_summary.id
            or draft.change_impact_snapshot_id != impact.id
            or draft.candidate_set_id != candidate_set.id
            or draft.behavior_memory_proposal_ids != proposal_ids
            or len(proposal_ids) != len(set(proposal_ids))
            or not set(draft.selected_action_candidate_ids) <= candidate_ids
        ):
            cls._bundle_conflict(draft)
        plan_lineage = (
            draft.root_plan_id,
            draft.source_revision,
            draft.source_plan_version,
        )
        if (
            plan_lineage
            != (
                impact.root_plan_id,
                impact.source_revision,
                impact.source_plan_version,
            )
            or plan_lineage
            != (
                candidate_set.root_plan_id,
                candidate_set.source_revision,
                candidate_set.source_plan_version,
            )
            or candidate_set.behavior_summary_id != behavior_summary.id
            or candidate_set.behavior_summary_fingerprint
            != behavior_summary.fingerprint
            or candidate_set.context_snapshot_reference_id
            != draft.context_snapshot_reference_id
            or candidate_set.change_impact_snapshot_id != impact.id
            or any(
                item.impact_snapshot_id != impact.id
                for item in candidate_set.candidates
            )
        ):
            cls._bundle_conflict(draft)
        if (
            trace.draft_id != draft.id
            or trace.request_id != draft.request_id
            or trace.behavior_summary_id != behavior_summary.id
            or trace.behavior_summary_fingerprint != behavior_summary.fingerprint
            or trace.context_snapshot_reference_id
            != draft.context_snapshot_reference_id
            or trace.change_impact_snapshot_id != impact.id
            or trace.candidate_set_id != candidate_set.id
            or trace.candidate_set_fingerprint != candidate_set.fingerprint
            or trace.scope_status is not draft.scope_status
            or trace.outcome is not draft.outcome
            or trace.fallback_used != draft.fallback_used
        ):
            cls._bundle_conflict(draft)
        pattern_ids = {
            item.pattern_id
            for group in (
                behavior_summary.repeated_time_patterns,
                behavior_summary.repeated_location_patterns,
                behavior_summary.repeated_skip_patterns,
            )
            for item in group
        }
        evidence_ids = {
            item.checkin_id for item in behavior_summary.evidence_references
        }
        for group in (
            behavior_summary.repeated_time_patterns,
            behavior_summary.repeated_location_patterns,
            behavior_summary.repeated_skip_patterns,
        ):
            if any(not set(item.evidence_ids) <= evidence_ids for item in group):
                cls._bundle_conflict(draft)
        if any(
            not set(item.evidence_pattern_ids) <= pattern_ids
            for item in candidate_set.candidates
        ):
            cls._bundle_conflict(draft)
        proposal_evidence = evidence_ids | set(behavior_summary.conflict_checkin_ids)
        if any(
            not set(item.behavior_pattern_ids) <= pattern_ids
            or not set(item.evidence_checkin_ids) <= proposal_evidence
            for item in proposals
        ):
            cls._bundle_conflict(draft)

    @staticmethod
    def _bundle_conflict(draft: RecoveryDraft) -> None:
        raise RepositoryConflictError(
            "RecoveryBundle",
            draft.id,
            expected_version=1,
            actual_version=draft.version,
        )

    @staticmethod
    def _summary_row(value: BehaviorSummary) -> RecoveryBehaviorSummaryModel:
        return RecoveryBehaviorSummaryModel(
            id=str(value.id),
            user_id=str(value.user_id),
            window_start_utc=MySQLRecoveryDraftRepository._db_time(
                value.window_start_utc
            ),
            window_end_utc=MySQLRecoveryDraftRepository._db_time(value.window_end_utc),
            timezone=value.timezone,
            scheduled_session_count=value.scheduled_session_count,
            checked_in_session_count=value.checked_in_session_count,
            completed_count=value.completed_count,
            partially_completed_count=value.partially_completed_count,
            skipped_count=value.skipped_count,
            missing_checkin_count=value.missing_checkin_count,
            completion_rate=value.completion_rate,
            participation_rate=value.participation_rate,
            rpe_sample_count=value.rpe_sample_count,
            average_reported_rpe=value.average_reported_rpe,
            high_reported_rpe_count=value.high_reported_rpe_count,
            repeated_time_patterns=[
                MySQLRecoveryDraftRepository._pattern_json(item)
                for item in value.repeated_time_patterns
            ],
            repeated_location_patterns=[
                MySQLRecoveryDraftRepository._pattern_json(item)
                for item in value.repeated_location_patterns
            ],
            repeated_skip_patterns=[
                MySQLRecoveryDraftRepository._pattern_json(item)
                for item in value.repeated_skip_patterns
            ],
            evidence_references=[
                MySQLRecoveryDraftRepository._evidence_json(item)
                for item in value.evidence_references
            ],
            conflict_checkin_ids=[str(item) for item in value.conflict_checkin_ids],
            policy_version=value.policy_version,
            fingerprint=value.fingerprint,
            created_at=MySQLRecoveryDraftRepository._db_time(value.created_at),
        )

    @classmethod
    def _summary_from_row(cls, row: RecoveryBehaviorSummaryModel) -> BehaviorSummary:
        return BehaviorSummary(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            window_start_utc=cls._utc(row.window_start_utc),
            window_end_utc=cls._utc(row.window_end_utc),
            timezone=row.timezone,
            scheduled_session_count=row.scheduled_session_count,
            checked_in_session_count=row.checked_in_session_count,
            completed_count=row.completed_count,
            partially_completed_count=row.partially_completed_count,
            skipped_count=row.skipped_count,
            missing_checkin_count=row.missing_checkin_count,
            completion_rate=row.completion_rate,
            participation_rate=row.participation_rate,
            rpe_sample_count=row.rpe_sample_count,
            average_reported_rpe=row.average_reported_rpe,
            high_reported_rpe_count=row.high_reported_rpe_count,
            repeated_time_patterns=tuple(
                cls._pattern_from_json(item) for item in row.repeated_time_patterns
            ),
            repeated_location_patterns=tuple(
                cls._pattern_from_json(item) for item in row.repeated_location_patterns
            ),
            repeated_skip_patterns=tuple(
                cls._pattern_from_json(item) for item in row.repeated_skip_patterns
            ),
            evidence_references=tuple(
                cls._evidence_from_json(item) for item in row.evidence_references
            ),
            conflict_checkin_ids=tuple(UUID(item) for item in row.conflict_checkin_ids),
            policy_version=row.policy_version,
            fingerprint=row.fingerprint,
            created_at=cls._utc(row.created_at),
        )

    @staticmethod
    def _impact_row(value: RecoveryChangeImpactSnapshot) -> RecoveryChangeImpactModel:
        return RecoveryChangeImpactModel(
            id=str(value.id),
            user_id=str(value.user_id),
            root_plan_id=str(value.root_plan_id),
            source_revision=value.source_revision,
            source_plan_version=value.source_plan_version,
            mutable_session_ids=[str(item) for item in value.mutable_session_ids],
            immutable_session_ids=[str(item) for item in value.immutable_session_ids],
            preserved_session_ids=[str(item) for item in value.preserved_session_ids],
            calendar_bound_session_ids=[
                str(item) for item in value.calendar_bound_session_ids
            ],
            completed_checkin_ids=[str(item) for item in value.completed_checkin_ids],
            weekly_frequency_before=value.weekly_frequency_before,
            minimum_allowed_frequency=value.minimum_allowed_frequency,
            maximum_allowed_frequency=value.maximum_allowed_frequency,
            requires_session_redesign=value.requires_session_redesign,
            requires_schedule_redraft=value.requires_schedule_redraft,
            requires_calendar_reconciliation=value.requires_calendar_reconciliation,
            requires_new_plan_revision=value.requires_new_plan_revision,
            fingerprint=value.fingerprint,
            created_at=MySQLRecoveryDraftRepository._db_time(value.created_at),
        )

    @classmethod
    def _impact_from_row(
        cls, row: RecoveryChangeImpactModel
    ) -> RecoveryChangeImpactSnapshot:
        return RecoveryChangeImpactSnapshot(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            root_plan_id=UUID(row.root_plan_id),
            source_revision=row.source_revision,
            source_plan_version=row.source_plan_version,
            mutable_session_ids=tuple(UUID(item) for item in row.mutable_session_ids),
            immutable_session_ids=tuple(
                UUID(item) for item in row.immutable_session_ids
            ),
            preserved_session_ids=tuple(
                UUID(item) for item in row.preserved_session_ids
            ),
            calendar_bound_session_ids=tuple(
                UUID(item) for item in row.calendar_bound_session_ids
            ),
            completed_checkin_ids=tuple(
                UUID(item) for item in row.completed_checkin_ids
            ),
            weekly_frequency_before=row.weekly_frequency_before,
            minimum_allowed_frequency=row.minimum_allowed_frequency,
            maximum_allowed_frequency=row.maximum_allowed_frequency,
            requires_session_redesign=row.requires_session_redesign,
            requires_schedule_redraft=row.requires_schedule_redraft,
            requires_calendar_reconciliation=row.requires_calendar_reconciliation,
            requires_new_plan_revision=row.requires_new_plan_revision,
            fingerprint=row.fingerprint,
            created_at=cls._utc(row.created_at),
        )

    @staticmethod
    def _candidate_set_row(
        value: RecoveryActionCandidateSet,
    ) -> RecoveryCandidateSetModel:
        return RecoveryCandidateSetModel(
            id=str(value.id),
            user_id=str(value.user_id),
            root_plan_id=str(value.root_plan_id),
            source_revision=value.source_revision,
            source_plan_version=value.source_plan_version,
            behavior_summary_id=str(value.behavior_summary_id),
            behavior_summary_fingerprint=value.behavior_summary_fingerprint,
            context_snapshot_reference_id=str(value.context_snapshot_reference_id),
            context_fingerprint=value.context_fingerprint,
            change_impact_snapshot_id=str(value.change_impact_snapshot_id),
            fingerprint=value.fingerprint,
            policy_version=value.policy_version,
            prompt_version=value.prompt_version,
            created_at=MySQLRecoveryDraftRepository._db_time(value.created_at),
        )

    @classmethod
    def _candidate_set_from_row(
        cls,
        row: RecoveryCandidateSetModel,
        candidates: Sequence[RecoveryActionCandidateModel],
    ) -> RecoveryActionCandidateSet:
        return RecoveryActionCandidateSet(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            root_plan_id=UUID(row.root_plan_id),
            source_revision=row.source_revision,
            source_plan_version=row.source_plan_version,
            behavior_summary_id=UUID(row.behavior_summary_id),
            behavior_summary_fingerprint=row.behavior_summary_fingerprint,
            context_snapshot_reference_id=UUID(row.context_snapshot_reference_id),
            context_fingerprint=row.context_fingerprint,
            change_impact_snapshot_id=UUID(row.change_impact_snapshot_id),
            candidates=tuple(cls._candidate_from_row(item) for item in candidates),
            fingerprint=row.fingerprint,
            policy_version=row.policy_version,
            prompt_version=row.prompt_version,
            created_at=cls._utc(row.created_at),
        )

    @staticmethod
    def _candidate_rows(
        user_id: UUID,
        candidate_set_id: UUID,
        values: tuple[RecoveryActionCandidate, ...],
    ) -> list[RecoveryActionCandidateModel]:
        return [
            RecoveryActionCandidateModel(
                id=str(value.id),
                user_id=str(user_id),
                candidate_set_id=str(candidate_set_id),
                sequence_no=index,
                action_type=value.action_type.value,
                target_session_id=(
                    None
                    if value.target_session_id is None
                    else str(value.target_session_id)
                ),
                target_week_start=value.target_week_start,
                redesign_goal=(
                    None if value.redesign_goal is None else value.redesign_goal.value
                ),
                evidence_pattern_ids=list(value.evidence_pattern_ids),
                impact_snapshot_id=str(value.impact_snapshot_id),
                requires_schedule_draft=value.requires_schedule_draft,
                requires_session_design_draft=value.requires_session_design_draft,
                requires_plan_revision=value.requires_plan_revision,
                requires_calendar_reconciliation=(
                    value.requires_calendar_reconciliation
                ),
                deterministic_rank=value.deterministic_rank,
            )
            for index, value in enumerate(values, start=1)
        ]

    @staticmethod
    def _candidate_from_row(
        row: RecoveryActionCandidateModel,
    ) -> RecoveryActionCandidate:
        return RecoveryActionCandidate(
            id=UUID(row.id),
            action_type=RecoveryActionType(row.action_type),
            target_session_id=(
                None if row.target_session_id is None else UUID(row.target_session_id)
            ),
            target_week_start=row.target_week_start,
            redesign_goal=(
                None
                if row.redesign_goal is None
                else RecoveryRedesignGoal(row.redesign_goal)
            ),
            evidence_pattern_ids=tuple(row.evidence_pattern_ids),
            impact_snapshot_id=UUID(row.impact_snapshot_id),
            requires_schedule_draft=row.requires_schedule_draft,
            requires_session_design_draft=row.requires_session_design_draft,
            requires_plan_revision=row.requires_plan_revision,
            requires_calendar_reconciliation=row.requires_calendar_reconciliation,
            deterministic_rank=row.deterministic_rank,
        )

    @staticmethod
    def _draft_row(value: RecoveryDraft) -> RecoveryDraftModel:
        return RecoveryDraftModel(
            id=str(value.id),
            request_id=str(value.request_id),
            client_request_id=value.client_request_id,
            user_id=str(value.user_id),
            request_payload_fingerprint=value.request_payload_fingerprint,
            request_fingerprint=value.request_fingerprint,
            root_plan_id=str(value.root_plan_id),
            source_revision=value.source_revision,
            source_plan_version=value.source_plan_version,
            behavior_summary_id=str(value.behavior_summary_id),
            context_snapshot_reference_id=str(value.context_snapshot_reference_id),
            change_impact_snapshot_id=str(value.change_impact_snapshot_id),
            candidate_set_id=str(value.candidate_set_id),
            selected_action_candidate_ids=[
                str(item) for item in value.selected_action_candidate_ids
            ],
            unresolved_session_ids=[str(item) for item in value.unresolved_session_ids],
            behavior_memory_proposal_ids=[
                str(item) for item in value.behavior_memory_proposal_ids
            ],
            explanation_summary=value.explanation_summary,
            outcome=value.outcome.value,
            source=value.source.value,
            fallback_used=value.fallback_used,
            scope_status=value.scope_status.value,
            status=value.status.value,
            created_at=MySQLRecoveryDraftRepository._db_time(value.created_at),
            expires_at=MySQLRecoveryDraftRepository._db_time(value.expires_at),
            version=value.version,
            reviewed_at=MySQLRecoveryDraftRepository._optional_db_time(
                value.reviewed_at
            ),
            application_result_id=(
                None
                if value.application_result_id is None
                else str(value.application_result_id)
            ),
            applied_root_plan_id=(
                None
                if value.applied_root_plan_id is None
                else str(value.applied_root_plan_id)
            ),
            applied_source_revision=value.applied_source_revision,
            applied_created_revision=value.applied_created_revision,
            applied_session_ids=[str(item) for item in value.applied_session_ids],
            created_session_design_draft_ids=[
                str(item) for item in value.created_session_design_draft_ids
            ],
            created_schedule_draft_ids=[
                str(item) for item in value.created_schedule_draft_ids
            ],
            applied_at=MySQLRecoveryDraftRepository._optional_db_time(value.applied_at),
        )

    @classmethod
    def _draft_from_row(cls, row: RecoveryDraftModel) -> RecoveryDraft:
        return RecoveryDraft(
            id=UUID(row.id),
            request_id=UUID(row.request_id),
            client_request_id=row.client_request_id,
            user_id=UUID(row.user_id),
            request_payload_fingerprint=row.request_payload_fingerprint,
            request_fingerprint=row.request_fingerprint,
            root_plan_id=UUID(row.root_plan_id),
            source_revision=row.source_revision,
            source_plan_version=row.source_plan_version,
            behavior_summary_id=UUID(row.behavior_summary_id),
            context_snapshot_reference_id=UUID(row.context_snapshot_reference_id),
            change_impact_snapshot_id=UUID(row.change_impact_snapshot_id),
            candidate_set_id=UUID(row.candidate_set_id),
            selected_action_candidate_ids=tuple(
                UUID(item) for item in row.selected_action_candidate_ids
            ),
            unresolved_session_ids=tuple(
                UUID(item) for item in row.unresolved_session_ids
            ),
            behavior_memory_proposal_ids=tuple(
                UUID(item) for item in row.behavior_memory_proposal_ids
            ),
            explanation_summary=row.explanation_summary,
            outcome=RecoveryDraftOutcome(row.outcome),
            source=RecoveryDraftSource(row.source),
            fallback_used=row.fallback_used,
            scope_status=RecoveryScopeStatus(row.scope_status),
            status=RecoveryDraftStatus(row.status),
            created_at=cls._utc(row.created_at),
            expires_at=cls._utc(row.expires_at),
            version=row.version,
            reviewed_at=(
                None if row.reviewed_at is None else cls._utc(row.reviewed_at)
            ),
            application_result_id=(
                None
                if row.application_result_id is None
                else UUID(row.application_result_id)
            ),
            applied_root_plan_id=(
                None
                if row.applied_root_plan_id is None
                else UUID(row.applied_root_plan_id)
            ),
            applied_source_revision=row.applied_source_revision,
            applied_created_revision=row.applied_created_revision,
            applied_session_ids=tuple(UUID(item) for item in row.applied_session_ids),
            created_session_design_draft_ids=tuple(
                UUID(item) for item in row.created_session_design_draft_ids
            ),
            created_schedule_draft_ids=tuple(
                UUID(item) for item in row.created_schedule_draft_ids
            ),
            applied_at=None if row.applied_at is None else cls._utc(row.applied_at),
        )

    @staticmethod
    def _apply_draft(row: RecoveryDraftModel, value: RecoveryDraft) -> None:
        row.status = value.status.value
        row.reviewed_at = MySQLRecoveryDraftRepository._optional_db_time(
            value.reviewed_at
        )
        row.application_result_id = (
            None
            if value.application_result_id is None
            else str(value.application_result_id)
        )
        row.applied_root_plan_id = (
            None
            if value.applied_root_plan_id is None
            else str(value.applied_root_plan_id)
        )
        row.applied_source_revision = value.applied_source_revision
        row.applied_created_revision = value.applied_created_revision
        row.applied_session_ids = [str(item) for item in value.applied_session_ids]
        row.created_session_design_draft_ids = [
            str(item) for item in value.created_session_design_draft_ids
        ]
        row.created_schedule_draft_ids = [
            str(item) for item in value.created_schedule_draft_ids
        ]
        row.applied_at = MySQLRecoveryDraftRepository._optional_db_time(
            value.applied_at
        )
        row.version = value.version

    @staticmethod
    def _immutable_draft_fields(value: RecoveryDraft) -> tuple[object, ...]:
        return (
            value.id,
            value.request_id,
            value.client_request_id,
            value.user_id,
            value.request_payload_fingerprint,
            value.request_fingerprint,
            value.root_plan_id,
            value.source_revision,
            value.source_plan_version,
            value.behavior_summary_id,
            value.context_snapshot_reference_id,
            value.change_impact_snapshot_id,
            value.candidate_set_id,
            value.selected_action_candidate_ids,
            value.unresolved_session_ids,
            value.behavior_memory_proposal_ids,
            value.explanation_summary,
            value.outcome,
            value.source,
            value.fallback_used,
            value.scope_status,
            value.created_at,
            value.expires_at,
        )

    @staticmethod
    def _proposal_rows(
        draft_id: UUID, values: tuple[BehaviorMemoryProposal, ...]
    ) -> list[RecoveryMemoryProposalModel]:
        return [
            RecoveryMemoryProposalModel(
                id=str(value.id),
                user_id=str(value.user_id),
                draft_id=str(draft_id),
                sequence_no=index,
                memory_type=value.memory_type.value,
                proposed_key=value.proposed_key,
                proposed_value=value.proposed_value,
                behavior_pattern_ids=list(value.behavior_pattern_ids),
                evidence_checkin_ids=[str(item) for item in value.evidence_checkin_ids],
                confidence_tier=value.confidence_tier.value,
                status=value.status.value,
                created_at=MySQLRecoveryDraftRepository._db_time(value.created_at),
                expires_at=MySQLRecoveryDraftRepository._db_time(value.expires_at),
            )
            for index, value in enumerate(values, start=1)
        ]

    @classmethod
    def _proposal_from_row(
        cls, row: RecoveryMemoryProposalModel
    ) -> BehaviorMemoryProposal:
        return BehaviorMemoryProposal(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            memory_type=MemoryType(row.memory_type),
            proposed_key=row.proposed_key,
            proposed_value=row.proposed_value,
            behavior_pattern_ids=tuple(row.behavior_pattern_ids),
            evidence_checkin_ids=tuple(UUID(item) for item in row.evidence_checkin_ids),
            confidence_tier=BehaviorConfidenceTier(row.confidence_tier),
            status=BehaviorMemoryProposalStatus(row.status),
            created_at=cls._utc(row.created_at),
            expires_at=cls._utc(row.expires_at),
        )

    @staticmethod
    def _trace_row(value: RecoveryTrace) -> RecoveryTraceModel:
        return RecoveryTraceModel(
            id=str(value.id),
            user_id=str(value.user_id),
            draft_id=str(value.draft_id),
            request_id=str(value.request_id),
            behavior_summary_id=str(value.behavior_summary_id),
            behavior_summary_fingerprint=value.behavior_summary_fingerprint,
            context_snapshot_reference_id=str(value.context_snapshot_reference_id),
            change_impact_snapshot_id=str(value.change_impact_snapshot_id),
            candidate_set_id=str(value.candidate_set_id),
            candidate_set_fingerprint=value.candidate_set_fingerprint,
            scope_status=value.scope_status.value,
            provider_name=value.provider_name,
            provider_version=value.provider_version,
            attempt_no=value.attempt_no,
            outcome=value.outcome.value,
            fallback_used=value.fallback_used,
            validation_error_code=value.validation_error_code,
            latency_ms=Decimal(str(value.latency_ms)),
            created_at=MySQLRecoveryDraftRepository._db_time(value.created_at),
        )

    @classmethod
    def _trace_from_row(cls, row: RecoveryTraceModel) -> RecoveryTrace:
        return RecoveryTrace(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            draft_id=UUID(row.draft_id),
            request_id=UUID(row.request_id),
            behavior_summary_id=UUID(row.behavior_summary_id),
            behavior_summary_fingerprint=row.behavior_summary_fingerprint,
            context_snapshot_reference_id=UUID(row.context_snapshot_reference_id),
            change_impact_snapshot_id=UUID(row.change_impact_snapshot_id),
            candidate_set_id=UUID(row.candidate_set_id),
            candidate_set_fingerprint=row.candidate_set_fingerprint,
            scope_status=RecoveryScopeStatus(row.scope_status),
            provider_name=row.provider_name,
            provider_version=row.provider_version,
            attempt_no=row.attempt_no,
            outcome=RecoveryDraftOutcome(row.outcome),
            fallback_used=row.fallback_used,
            validation_error_code=row.validation_error_code,
            latency_ms=float(row.latency_ms),
            created_at=cls._utc(row.created_at),
        )

    @staticmethod
    def _pattern_json(value: BehaviorPattern) -> dict[str, Any]:
        return {
            "pattern_id": value.pattern_id,
            "pattern_type": value.pattern_type.value,
            "key": value.key,
            "occurrence_count": value.occurrence_count,
            "opportunity_count": value.opportunity_count,
            "ratio": str(value.ratio),
            "evidence_ids": [str(item) for item in value.evidence_ids],
        }

    @staticmethod
    def _pattern_from_json(value: dict[str, Any]) -> BehaviorPattern:
        return BehaviorPattern(
            pattern_id=str(value["pattern_id"]),
            pattern_type=BehaviorPatternType(str(value["pattern_type"])),
            key=str(value["key"]),
            occurrence_count=int(value["occurrence_count"]),
            opportunity_count=int(value["opportunity_count"]),
            ratio=Decimal(str(value["ratio"])),
            evidence_ids=tuple(UUID(str(item)) for item in value["evidence_ids"]),
        )

    @staticmethod
    def _evidence_json(value: BehaviorEvidenceReference) -> dict[str, Any]:
        return {
            "checkin_id": str(value.checkin_id),
            "logical_session_id": str(value.logical_session_id),
            "root_plan_id": str(value.root_plan_id),
            "plan_revision": value.plan_revision,
            "scheduled_at_utc": value.scheduled_at_utc.isoformat(),
            "status": value.status.value,
            "reported_rpe": value.reported_rpe,
            "occurred_at": value.occurred_at.isoformat(),
            "scheduled_weekday": value.scheduled_weekday,
            "scheduled_time_bucket": value.scheduled_time_bucket,
            "location": value.location,
            "fingerprint": value.fingerprint,
        }

    @staticmethod
    def _evidence_from_json(value: dict[str, Any]) -> BehaviorEvidenceReference:
        return BehaviorEvidenceReference(
            checkin_id=UUID(str(value["checkin_id"])),
            logical_session_id=UUID(str(value["logical_session_id"])),
            root_plan_id=UUID(str(value["root_plan_id"])),
            plan_revision=int(value["plan_revision"]),
            scheduled_at_utc=MySQLRecoveryDraftRepository._json_time(
                str(value["scheduled_at_utc"])
            ),
            status=CheckInStatus(str(value["status"])),
            reported_rpe=(
                None if value["reported_rpe"] is None else int(value["reported_rpe"])
            ),
            occurred_at=MySQLRecoveryDraftRepository._json_time(
                str(value["occurred_at"])
            ),
            scheduled_weekday=str(value["scheduled_weekday"]),
            scheduled_time_bucket=str(value["scheduled_time_bucket"]),
            location=str(value["location"]),
            fingerprint=str(value["fingerprint"]),
        )

    @staticmethod
    def _json_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return (
            parsed.replace(tzinfo=UTC)
            if parsed.tzinfo is None
            else parsed.astimezone(UTC)
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)

    @staticmethod
    def _optional_db_time(value: datetime | None) -> datetime | None:
        return None if value is None else MySQLRecoveryDraftRepository._db_time(value)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )
