"""Atomic single-process commit for a complete Recovery application."""

from copy import deepcopy
from uuid import UUID

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
from app.persistence.memory.store import InMemoryStore, StoredSession


class InMemoryRecoveryApplicationRepository:
    """Keep CAS checks and writes atomic without moving business merge under lock."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self._fail_next_commit = False

    def fail_next_commit_for_test(self) -> None:
        self._fail_next_commit = True

    async def get_result(
        self, user_id: UUID, result_id: UUID
    ) -> RecoveryApplicationResult | None:
        async with self._store.lock:
            value = self._store._recovery_application_results.get(result_id)
            return deepcopy(value) if value and value.user_id == user_id else None

    async def get_result_by_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> RecoveryApplicationResult | None:
        async with self._store.lock:
            result_id = self._store._recovery_application_id_by_draft.get(draft_id)
            value = (
                self._store._recovery_application_results.get(result_id)
                if result_id is not None
                else None
            )
            return deepcopy(value) if value and value.user_id == user_id else None

    async def get_result_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> RecoveryApplicationResult | None:
        async with self._store.lock:
            result_id = self._store._recovery_application_id_by_request.get(
                (user_id, client_request_id)
            )
            return (
                deepcopy(self._store._recovery_application_results[result_id])
                if result_id is not None
                else None
            )

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
        async with self._store.lock:
            existing_id = self._store._recovery_application_id_by_request.get(
                request_key
            )
            if existing_id is not None:
                existing = self._store._recovery_application_results[existing_id]
                if existing.application_fingerprint != result.application_fingerprint:
                    raise RepositoryUniqueError(
                        "recovery_application.user_request", request_key
                    )
                plan_id = None
                if existing.created_revision is not None:
                    plan_id = self._store._plan_id_by_revision.get(
                        (existing.user_id, source.week_start, existing.created_revision)
                    )
                return RecoveryApplicationCommit(
                    result=deepcopy(existing),
                    plan_revision_id=plan_id,
                    created=False,
                )
            current_draft = self._store._recovery_drafts.get(expected_draft.id)
            if (
                current_draft is None
                or current_draft.user_id != result.user_id
                or current_draft.version != expected_draft.version
                or current_draft.status != expected_draft.status
            ):
                raise RepositoryConflictError(
                    "RecoveryDraft",
                    expected_draft.id,
                    expected_version=expected_draft.version,
                    actual_version=-1
                    if current_draft is None
                    else current_draft.version,
                )
            stored_source = self._store._plans.get(source.id)
            if stored_source is None or stored_source.version != source.version:
                raise RepositoryConflictError(
                    "WeeklyPlan",
                    source.id,
                    expected_version=source.version,
                    actual_version=-1
                    if stored_source is None
                    else stored_source.version,
                )
            confirmed = sorted(
                (
                    item
                    for item in self._store._plans.values()
                    if item.user_id == source.user_id
                    and item.series_id == source.series_id
                    and item.status is WeeklyPlanStatus.CONFIRMED
                ),
                key=lambda item: (item.revision, str(item.id)),
                reverse=True,
            )
            if not confirmed or confirmed[0].id != source.id:
                raise RepositoryConflictError(
                    "WeeklyPlan.current_revision",
                    source.series_id,
                    expected_version=source.revision,
                    actual_version=confirmed[0].revision if confirmed else -1,
                )
            if expected_draft.id in self._store._recovery_application_id_by_draft:
                raise RepositoryUniqueError(
                    "recovery_application.draft", expected_draft.id
                )
            if revision is not None:
                revision_key = (
                    revision.user_id,
                    revision.week_start,
                    revision.revision,
                )
                if revision_key in self._store._plan_id_by_revision:
                    raise RepositoryUniqueError(
                        "weekly_plan.user_week_revision", revision_key
                    )
            for design_child in applied_session_design_drafts:
                current_design = self._store._session_design_drafts.get(design_child.id)
                if (
                    current_design is None
                    or design_child.version != current_design.version + 1
                ):
                    raise RepositoryConflictError(
                        "SessionDesignDraft",
                        design_child.id,
                        expected_version=design_child.version - 1,
                        actual_version=(
                            -1 if current_design is None else current_design.version
                        ),
                    )
            for schedule_child in applied_schedule_drafts:
                current_schedule = self._store._schedule_drafts.get(schedule_child.id)
                if (
                    current_schedule is None
                    or schedule_child.version != current_schedule.version + 1
                ):
                    raise RepositoryConflictError(
                        "ScheduleDraft",
                        schedule_child.id,
                        expected_version=schedule_child.version - 1,
                        actual_version=(
                            -1 if current_schedule is None else current_schedule.version
                        ),
                    )
            if self._fail_next_commit:
                self._fail_next_commit = False
                raise RuntimeError("Injected atomic Recovery application failure.")
            if revision is not None:
                revision_snapshot = deepcopy(revision)
                revision_key = (
                    revision.user_id,
                    revision.week_start,
                    revision.revision,
                )
                self._store._plans[revision.id] = revision_snapshot
                self._store._plan_id_by_revision[revision_key] = revision.id
                for session in revision_snapshot.sessions:
                    self._store._sessions[(revision.id, session.id)] = StoredSession(
                        user_id=revision.user_id,
                        plan_id=revision.id,
                        session=deepcopy(session),
                    )
            result_snapshot = deepcopy(result)
            self._store._recovery_application_results[result.id] = result_snapshot
            self._store._recovery_application_id_by_request[request_key] = result.id
            self._store._recovery_application_id_by_draft[expected_draft.id] = result.id
            self._store._recovery_drafts[expected_draft.id] = deepcopy(applied_draft)
            for design_child in applied_session_design_drafts:
                self._store._session_design_drafts[design_child.id] = deepcopy(
                    design_child
                )
            for schedule_child in applied_schedule_drafts:
                self._store._schedule_drafts[schedule_child.id] = deepcopy(
                    schedule_child
                )
            return RecoveryApplicationCommit(
                result=deepcopy(result_snapshot),
                plan_revision_id=revision.id if revision is not None else None,
                created=True,
            )

    async def bind_session_design_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID, child_draft_id: UUID
    ) -> None:
        key = (recovery_draft_id, candidate_id)
        async with self._store.lock:
            existing = self._store._recovery_session_design_drafts.get(key)
            if existing not in {None, child_draft_id}:
                raise RepositoryUniqueError("recovery.session_design_subdraft", key)
            self._store._recovery_session_design_drafts[key] = child_draft_id

    async def get_session_design_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID
    ) -> UUID | None:
        async with self._store.lock:
            return self._store._recovery_session_design_drafts.get(
                (recovery_draft_id, candidate_id)
            )

    async def bind_schedule_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID, child_draft_id: UUID
    ) -> None:
        key = (recovery_draft_id, candidate_id)
        async with self._store.lock:
            existing = self._store._recovery_schedule_drafts.get(key)
            if existing not in {None, child_draft_id}:
                raise RepositoryUniqueError("recovery.schedule_subdraft", key)
            self._store._recovery_schedule_drafts[key] = child_draft_id

    async def get_schedule_subdraft(
        self, recovery_draft_id: UUID, candidate_id: UUID
    ) -> UUID | None:
        async with self._store.lock:
            return self._store._recovery_schedule_drafts.get(
                (recovery_draft_id, candidate_id)
            )

    async def get_memory_import(
        self, user_id: UUID, client_request_id: str
    ) -> RecoveryMemoryProposalImportResult | None:
        async with self._store.lock:
            value = self._store._recovery_memory_imports.get(
                (user_id, client_request_id)
            )
            return deepcopy(value)

    async def save_memory_import(
        self, result: RecoveryMemoryProposalImportResult
    ) -> RecoveryMemoryProposalImportResult:
        key = (result.user_id, result.client_request_id)
        async with self._store.lock:
            existing = self._store._recovery_memory_imports.get(key)
            if existing is not None:
                if existing.fingerprint != result.fingerprint:
                    raise RepositoryUniqueError("recovery.memory_import", key)
                return deepcopy(existing)
            self._store._recovery_memory_imports[key] = deepcopy(result)
            return deepcopy(result)
