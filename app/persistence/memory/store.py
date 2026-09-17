"""Shared, resettable process-local storage for phase 1A repositories."""

import asyncio
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final
from uuid import UUID

from app.domain.behavior.models import BehaviorMemoryProposal, BehaviorSummary
from app.domain.calendar_operations.models import (
    CalendarEventBinding,
    CalendarOperationAttempt,
    CalendarOperationDraft,
)
from app.domain.checkins.models import WorkoutCheckIn
from app.domain.common import (
    RepositoryConflictError as RepositoryConflictError,
)
from app.domain.common import (
    RepositoryError as RepositoryError,
)
from app.domain.common import (
    RepositoryUniqueError as RepositoryUniqueError,
)
from app.domain.context.enums import AgentType
from app.domain.context.models import ContextBuildAudit, FrozenContextSnapshot
from app.domain.exercises.models import Exercise
from app.domain.ics.models import IcsExportRecord
from app.domain.memory.enums import MemoryType
from app.domain.memory.models import MemoryCandidate, MemoryEvidence, UserMemory
from app.domain.plans.models import WeeklyPlan
from app.domain.profile_agent.apply_models import ProfileDraftApplyResult
from app.domain.profile_agent.memory_candidates import DraftMemoryCandidateImportResult
from app.domain.profile_agent.models import ProfileAgentDraft
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.domain.recovery.models import (
    RecoveryActionCandidateSet,
    RecoveryChangeImpactSnapshot,
    RecoveryDraft,
    RecoveryTrace,
)
from app.domain.recovery_application.models import (
    RecoveryApplicationResult,
    RecoveryMemoryProposalImportResult,
)
from app.domain.schedule_application.models import ScheduleApplicationResult
from app.domain.scheduling.models import (
    BusySnapshot,
    ScheduleDraft,
    ScheduleTrace,
    TimeSlotCandidateSet,
)
from app.domain.session_design.models import (
    ExerciseCandidateSet,
    SessionDesignDraft,
    SessionDesignTrace,
)
from app.domain.session_design_application.models import SessionDesignApplicationResult
from app.domain.sessions.models import WorkoutSession


@dataclass(frozen=True, slots=True)
class StoredSession:
    """Associate a session snapshot with its owning user and plan."""

    user_id: UUID
    plan_id: UUID
    session: WorkoutSession


class InMemoryStore:
    """Own all mutable state used by the in-memory repository adapters.

    The store lives only for the lifetime of the application process. Its
    dictionaries are intentionally private and are never exposed to API code.
    A single lock makes compound uniqueness/version checks atomic across all
    repository instances that share this object.
    """

    _FIRST_VERSION: Final = 1

    def __init__(self, exercises: Iterable[Exercise] = ()) -> None:
        self.lock = asyncio.Lock()
        self._seed_exercises = tuple(deepcopy(tuple(exercises)))
        self._profiles: dict[UUID, FitnessProfile] = {}
        self._profile_id_by_user: dict[UUID, UUID] = {}
        self._constraints: dict[UUID, UserConstraint] = {}
        self._exercises: dict[str, Exercise] = {}
        self._plans: dict[UUID, WeeklyPlan] = {}
        self._plan_id_by_revision: dict[tuple[UUID, date, int], UUID] = {}
        self._sessions: dict[tuple[UUID, UUID], StoredSession] = {}
        self._plan_id_by_replan_request: dict[tuple[UUID, str], UUID] = {}
        self._plan_generation_requests: dict[tuple[UUID, str], tuple[str, UUID]] = {}
        self._check_ins: dict[UUID, WorkoutCheckIn] = {}
        self._check_in_id_by_user_event: dict[tuple[UUID, str], UUID] = {}
        self._check_in_id_by_session: dict[UUID, UUID] = {}
        self._profile_agent_drafts: dict[UUID, ProfileAgentDraft] = {}
        self._session_candidate_sets: dict[UUID, ExerciseCandidateSet] = {}
        self._session_design_drafts: dict[UUID, SessionDesignDraft] = {}
        self._session_design_id_by_request: dict[tuple[UUID, str], UUID] = {}
        self._session_design_traces: dict[UUID, SessionDesignTrace] = {}
        self._session_design_application_results: dict[
            UUID, SessionDesignApplicationResult
        ] = {}
        self._session_design_application_id_by_request: dict[
            tuple[UUID, str], UUID
        ] = {}
        self._session_design_application_id_by_draft: dict[UUID, UUID] = {}
        self._profile_agent_draft_id_by_request: dict[tuple[UUID, str], UUID] = {}
        self._profile_draft_apply_results: dict[UUID, ProfileDraftApplyResult] = {}
        self._profile_draft_apply_id_by_request: dict[tuple[UUID, str], UUID] = {}
        self._profile_draft_apply_id_by_draft: dict[UUID, UUID] = {}
        self._profile_draft_reject_by_request: dict[
            tuple[UUID, str], tuple[UUID, str, datetime]
        ] = {}
        self._draft_memory_candidate_imports: dict[
            tuple[UUID, str], tuple[str, DraftMemoryCandidateImportResult]
        ] = {}
        self._memories: dict[UUID, UserMemory] = {}
        self._memory_evidence: dict[UUID, list[MemoryEvidence]] = {}
        self._memory_request_index: dict[
            tuple[UUID, str, str], tuple[str, UUID, UUID | None]
        ] = {}
        self._active_memory_exact: dict[tuple[UUID, MemoryType, str, str], UUID] = {}
        self._active_memory_single: dict[tuple[UUID, MemoryType, str], UUID] = {}
        self._memory_candidates: dict[UUID, MemoryCandidate] = {}
        self._candidate_request_index: dict[
            tuple[UUID, str, str], tuple[str, UUID, UUID | None]
        ] = {}
        self._candidate_by_source_content: dict[
            tuple[UUID, str, MemoryType, str, str], UUID
        ] = {}
        self._context_audits: dict[UUID, ContextBuildAudit] = {}
        self._context_snapshots: dict[UUID, FrozenContextSnapshot] = {}
        self._context_snapshot_by_scope: dict[tuple[UUID, AgentType, str], UUID] = {}
        self._busy_snapshots: dict[UUID, BusySnapshot] = {}
        self._time_slot_candidate_sets: dict[UUID, TimeSlotCandidateSet] = {}
        self._schedule_drafts: dict[UUID, ScheduleDraft] = {}
        self._schedule_draft_id_by_request: dict[tuple[UUID, str], UUID] = {}
        self._schedule_traces: dict[UUID, ScheduleTrace] = {}
        self._schedule_application_results: dict[UUID, ScheduleApplicationResult] = {}
        self._schedule_application_id_by_request: dict[tuple[UUID, str], UUID] = {}
        self._schedule_application_id_by_draft: dict[UUID, UUID] = {}
        self._ics_exports: dict[UUID, IcsExportRecord] = {}
        self._ics_export_id_by_request: dict[tuple[UUID, str], UUID] = {}
        self._calendar_operation_drafts: dict[UUID, CalendarOperationDraft] = {}
        self._calendar_operation_id_by_request: dict[tuple[UUID, str], UUID] = {}
        self._calendar_bindings: dict[UUID, CalendarEventBinding] = {}
        self._calendar_binding_id_by_key: dict[
            tuple[UUID, str, str, UUID, UUID], UUID
        ] = {}
        self._calendar_operation_attempts: dict[UUID, CalendarOperationAttempt] = {}
        self._recovery_drafts: dict[UUID, RecoveryDraft] = {}
        self._recovery_draft_id_by_request: dict[tuple[UUID, str], UUID] = {}
        self._behavior_summaries: dict[UUID, BehaviorSummary] = {}
        self._recovery_impacts: dict[UUID, RecoveryChangeImpactSnapshot] = {}
        self._recovery_candidate_sets: dict[UUID, RecoveryActionCandidateSet] = {}
        self._recovery_traces: dict[UUID, RecoveryTrace] = {}
        self._behavior_memory_proposals: dict[UUID, BehaviorMemoryProposal] = {}
        self._recovery_application_results: dict[UUID, RecoveryApplicationResult] = {}
        self._recovery_application_id_by_request: dict[tuple[UUID, str], UUID] = {}
        self._recovery_application_id_by_draft: dict[UUID, UUID] = {}
        self._recovery_session_design_drafts: dict[tuple[UUID, UUID], UUID] = {}
        self._recovery_schedule_drafts: dict[tuple[UUID, UUID], UUID] = {}
        self._recovery_memory_imports: dict[
            tuple[UUID, str], RecoveryMemoryProposalImportResult
        ] = {}
        self._memory_query_failures_remaining = 0
        self._load_exercises(self._seed_exercises)

    def _load_exercises(self, exercises: Iterable[Exercise]) -> None:
        for exercise in exercises:
            if exercise.id in self._exercises:
                raise RepositoryUniqueError("exercise.id", exercise.id)
            self._exercises[exercise.id] = deepcopy(exercise)

    async def clear(self) -> None:
        """Remove all state, including catalog entries, for isolated tests."""

        async with self.lock:
            self._clear_unlocked()

    async def reset(self) -> None:
        """Remove runtime state and restore the constructor's catalog seed."""

        async with self.lock:
            self._clear_unlocked()
            self._load_exercises(self._seed_exercises)

    def _clear_unlocked(self) -> None:
        self._profiles.clear()
        self._profile_id_by_user.clear()
        self._constraints.clear()
        self._exercises.clear()
        self._plans.clear()
        self._plan_id_by_revision.clear()
        self._sessions.clear()
        self._plan_id_by_replan_request.clear()
        self._plan_generation_requests.clear()
        self._check_ins.clear()
        self._check_in_id_by_user_event.clear()
        self._check_in_id_by_session.clear()
        self._profile_agent_drafts.clear()
        self._session_candidate_sets.clear()
        self._session_design_drafts.clear()
        self._session_design_id_by_request.clear()
        self._session_design_traces.clear()
        self._session_design_application_results.clear()
        self._session_design_application_id_by_request.clear()
        self._session_design_application_id_by_draft.clear()
        self._profile_agent_draft_id_by_request.clear()
        self._profile_draft_apply_results.clear()
        self._profile_draft_apply_id_by_request.clear()
        self._profile_draft_apply_id_by_draft.clear()
        self._profile_draft_reject_by_request.clear()
        self._draft_memory_candidate_imports.clear()
        self._memories.clear()
        self._memory_evidence.clear()
        self._memory_request_index.clear()
        self._active_memory_exact.clear()
        self._active_memory_single.clear()
        self._memory_candidates.clear()
        self._candidate_request_index.clear()
        self._candidate_by_source_content.clear()
        self._context_audits.clear()
        self._context_snapshots.clear()
        self._context_snapshot_by_scope.clear()
        self._busy_snapshots.clear()
        self._time_slot_candidate_sets.clear()
        self._schedule_drafts.clear()
        self._schedule_draft_id_by_request.clear()
        self._schedule_traces.clear()
        self._schedule_application_results.clear()
        self._schedule_application_id_by_request.clear()
        self._schedule_application_id_by_draft.clear()
        self._ics_exports.clear()
        self._ics_export_id_by_request.clear()
        self._calendar_operation_drafts.clear()
        self._calendar_operation_id_by_request.clear()
        self._calendar_bindings.clear()
        self._calendar_binding_id_by_key.clear()
        self._calendar_operation_attempts.clear()
        self._recovery_drafts.clear()
        self._recovery_draft_id_by_request.clear()
        self._behavior_summaries.clear()
        self._recovery_impacts.clear()
        self._recovery_candidate_sets.clear()
        self._recovery_traces.clear()
        self._behavior_memory_proposals.clear()
        self._recovery_application_results.clear()
        self._recovery_application_id_by_request.clear()
        self._recovery_application_id_by_draft.clear()
        self._recovery_session_design_drafts.clear()
        self._recovery_schedule_drafts.clear()
        self._recovery_memory_imports.clear()
        self._memory_query_failures_remaining = 0

    @classmethod
    def require_first_version(
        cls, resource: str, resource_id: object, version: int
    ) -> None:
        """Enforce the first-write version without duplicating repository logic."""

        if version != cls._FIRST_VERSION:
            raise RepositoryConflictError(
                resource,
                resource_id,
                expected_version=cls._FIRST_VERSION,
                actual_version=version,
            )

    @staticmethod
    def require_next_version(
        resource: str,
        resource_id: object,
        *,
        current_version: int,
        incoming_version: int,
    ) -> None:
        """Require an update to advance exactly one optimistic-lock version."""

        expected_version = current_version + 1
        if incoming_version != expected_version:
            raise RepositoryConflictError(
                resource,
                resource_id,
                expected_version=expected_version,
                actual_version=incoming_version,
            )
