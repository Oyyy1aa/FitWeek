"""Recovery action resolution, child review, atomic revision, Memory, and Calendar."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from app.application.calendar_operations import (
    CalendarOperationService,
    CreateCalendarOperationCommand,
)
from app.application.errors import (
    RecoveryApplicationNotFound,
    RecoveryApplyIdempotencyConflict,
    RecoveryDraftAlreadyApplied,
    RecoveryDraftNotAccepted,
    RecoveryDraftNotFound,
    RecoveryDraftVersionConflict,
    RecoveryInvalidActionSelection,
    RecoveryMemoryProposalConflict,
    RecoveryMemoryProposalIdempotencyConflict,
    RecoveryMemoryProposalInvalid,
    RecoveryPlanApplicationFailed,
    RecoveryPlanSafetyFailed,
    RecoveryPlanVersionConflict,
    RecoverySourcePlanNotFound,
    RecoverySourceRevisionNotCurrent,
    RecoverySubdraftReviewRequired,
    RecoveryTargetSessionImmutable,
)
from app.application.memories import MemoryApplicationService
from app.application.schedules import ScheduleDraftService
from app.application.session_designs import SessionDesignService
from app.domain.behavior.models import BehaviorMemoryProposal
from app.domain.calendar_operations.models import CalendarOperationDraft
from app.domain.checkins.models import WorkoutCheckIn
from app.domain.checkins.repositories import CheckInRepository
from app.domain.common import (
    RepositoryConflictError,
    RepositoryUniqueError,
    utc_now,
)
from app.domain.exercises.models import Exercise
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.memory.enums import MemorySource
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.profiles.models import FitnessGoal
from app.domain.profiles.repositories import ProfileRepository
from app.domain.recovery.enums import (
    RecoveryDraftOutcome,
    RecoveryDraftStatus,
    RecoveryRedesignGoal,
    RecoveryScopeStatus,
)
from app.domain.recovery.models import (
    RecoveryActionCandidate,
    RecoveryActionCandidateSet,
    RecoveryDraft,
)
from app.domain.recovery.repositories import RecoveryDraftRepository
from app.domain.recovery_application.enums import RecoveryApplicationOutcome
from app.domain.recovery_application.models import (
    ACTION_RESOLUTION_POLICY_VERSION,
    PLAN_APPLICATION_POLICY_VERSION,
    ApplyRecoveryDraftCommand,
    ImportRecoveryMemoryProposalsCommand,
    RecoveryApplicationResult,
    RecoveryApplyPreview,
    RecoveryApplyValidation,
    RecoveryMemoryProposalImportResult,
    RecoveryMemoryProposalPreview,
)
from app.domain.recovery_application.repositories import RecoveryApplicationRepository
from app.domain.scheduling.models import (
    AvailabilityWindow,
    CreateScheduleDraftCommand,
    ScheduleDraft,
)
from app.domain.scheduling.repositories import ScheduleDraftRepository
from app.domain.session_design.models import SessionDesignDraft, SessionDesignRequest
from app.domain.session_design.repositories import SessionDesignRepository
from app.domain.sessions.models import WorkoutSession
from app.domain.tools.enums import ToolCaller, ToolId, ToolInvocationStatus
from app.domain.tools.models import ToolInvocationContext
from app.domain.users.models import UserAccount
from app.memory.candidate_service import CreateCandidateCommand
from app.recovery_application.memory_policy import (
    BehaviorMemoryProposalImportPolicy,
)
from app.recovery_application.merge_policy import RecoveryPlanMergePolicy
from app.recovery_application.resolution_policy import (
    RecoveryActionResolutionPolicy,
    ResolvedRecoveryActions,
)
from app.safety.engine import SafetyEngine
from app.tool_adapters.contracts import RecoverySpacingRequest, RecoverySpacingResponse
from app.tool_gateway.gateway import ToolGateway


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoverySubdrafts:
    session_design_draft_ids: tuple[UUID, ...]
    schedule_draft_ids: tuple[UUID, ...]
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryApplyOutcome:
    result: RecoveryApplicationResult
    plan: WeeklyPlan | None
    draft: RecoveryDraft
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryMemoryImportOutcome:
    result: RecoveryMemoryProposalImportResult
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class _Prepared:
    source: WeeklyPlan
    draft: RecoveryDraft
    candidate_set: RecoveryActionCandidateSet
    resolved: ResolvedRecoveryActions
    check_ins: tuple[WorkoutCheckIn, ...]
    session_design_drafts: tuple[SessionDesignDraft, ...]
    session_design_targets: dict[UUID, UUID]
    schedule_drafts: tuple[ScheduleDraft, ...]
    request_fingerprint: str
    application_fingerprint: str
    preview: RecoveryApplyPreview


class RecoveryApplicationService:
    """Never calls Recovery Agent; only applies frozen, reviewed references."""

    def __init__(
        self,
        *,
        drafts: RecoveryDraftRepository,
        applications: RecoveryApplicationRepository,
        plans: PlanRepository,
        profiles: ProfileRepository,
        exercises: ExerciseRepository,
        check_ins: CheckInRepository,
        session_designs: SessionDesignRepository,
        schedules: ScheduleDraftRepository,
        session_design_service: SessionDesignService,
        schedule_service: ScheduleDraftService,
        memories: MemoryApplicationService,
        calendar_operations: CalendarOperationService,
        safety: SafetyEngine,
        tool_gateway: ToolGateway | None = None,
    ) -> None:
        self._drafts = drafts
        self._applications = applications
        self._plans = plans
        self._profiles = profiles
        self._exercises = exercises
        self._check_ins = check_ins
        self._session_designs = session_designs
        self._schedules = schedules
        self._session_design_service = session_design_service
        self._schedule_service = schedule_service
        self._memories = memories
        self._calendar = calendar_operations
        self._memory_import_policy = BehaviorMemoryProposalImportPolicy()
        self._safety = safety
        self._tool_gateway = tool_gateway
        self._resolution = RecoveryActionResolutionPolicy()
        self._merge = RecoveryPlanMergePolicy()

    def attach_tool_gateway(self, gateway: ToolGateway) -> None:
        self._tool_gateway = gateway

    async def preview(
        self,
        user: UserAccount,
        draft_id: UUID,
        command: ApplyRecoveryDraftCommand,
    ) -> RecoveryApplyPreview:
        return (await self._prepare(user, draft_id, command)).preview

    async def create_subdrafts(
        self,
        user: UserAccount,
        draft_id: UUID,
        command: ApplyRecoveryDraftCommand,
    ) -> RecoverySubdrafts:
        prepared = await self._prepare(user, draft_id, command, require_children=False)
        created = False
        design_ids: list[UUID] = []
        schedule_ids: list[UUID] = []
        sessions = {item.id: item for item in prepared.source.sessions}
        candidates = {item.id: item for item in prepared.resolved.actions}
        for candidate_id in command.selected_action_candidate_ids:
            candidate = candidates[candidate_id]
            target_id = candidate.target_session_id
            if target_id is None:
                continue
            target = sessions[target_id]
            if candidate.requires_session_design_draft:
                bound = await self._applications.get_session_design_subdraft(
                    draft_id, candidate.id
                )
                if bound is None:
                    design_child, reused = await self._session_design_service.create(
                        user,
                        self._session_design_command(prepared.draft, candidate, target),
                    )
                    await self._applications.bind_session_design_subdraft(
                        draft_id, candidate.id, design_child.id
                    )
                    bound = design_child.id
                    created = created or not reused
                design_ids.append(bound)
            if candidate.requires_schedule_draft:
                bound = await self._applications.get_schedule_subdraft(
                    draft_id, candidate.id
                )
                if bound is None:
                    schedule_child, reused = await self._schedule_service.create(
                        user,
                        self._schedule_command(
                            user, prepared.draft, candidate, prepared.source, target
                        ),
                    )
                    await self._applications.bind_schedule_subdraft(
                        draft_id, candidate.id, schedule_child.id
                    )
                    bound = schedule_child.id
                    created = created or not reused
                schedule_ids.append(bound)
        return RecoverySubdrafts(
            session_design_draft_ids=tuple(sorted(design_ids, key=str)),
            schedule_draft_ids=tuple(sorted(schedule_ids, key=str)),
            created=created,
        )

    async def apply(
        self,
        user: UserAccount,
        draft_id: UUID,
        command: ApplyRecoveryDraftCommand,
    ) -> RecoveryApplyOutcome:
        existing = await self._applications.get_result_by_request(
            user.id, command.client_request_id
        )
        request_fingerprint = self._request_fingerprint(user.id, draft_id, command)
        if existing is not None:
            if existing.request_fingerprint != request_fingerprint:
                raise RecoveryApplyIdempotencyConflict(
                    "The Recovery apply request ID was reused with another payload."
                )
            return await self._outcome(user, existing, created=False)
        prepared = await self._prepare(user, draft_id, command, require_children=True)
        now = utc_now()
        revision: WeeklyPlan | None = None
        if not prepared.resolved.no_change and not prepared.resolved.next_week_review:
            revision = self._merge.build(
                source=prepared.source,
                recovery_draft=prepared.draft,
                candidate_set=prepared.candidate_set,
                resolved=prepared.resolved,
                session_design_drafts=prepared.session_design_drafts,
                session_design_targets=prepared.session_design_targets,
                schedule_drafts=prepared.schedule_drafts,
                check_ins=prepared.check_ins,
                fingerprint=prepared.application_fingerprint,
                created_at=now,
            )
            profile = await self._profiles.get_by_user_id(user.id)
            if profile is None:
                raise RecoveryPlanSafetyFailed("Fitness Profile was not found.")
            constraints = tuple(await self._profiles.list_constraints(profile.id))
            catalog = await self._catalog(revision)
            safety_profile = (
                replace(profile, weekly_frequency=len(revision.sessions))
                if prepared.resolved.remove_session_ids
                else profile
            )
            validation = self._safety.validate_plan(
                profile=safety_profile,
                constraints=constraints,
                plan=revision,
                exercise_catalog=catalog,
            )
            if not validation.passed:
                raise RecoveryPlanSafetyFailed(
                    "The complete Recovery Plan failed the deterministic Safety gate.",
                    violations=validation.violations,
                )
        outcome = (
            RecoveryApplicationOutcome.NO_CHANGE
            if prepared.resolved.no_change
            else (
                RecoveryApplicationOutcome.NEXT_WEEK_REVIEW_CREATED
                if prepared.resolved.next_week_review
                else RecoveryApplicationOutcome.PLAN_REVISION_CREATED
            )
        )
        result = RecoveryApplicationResult(
            id=uuid5(
                NAMESPACE_URL,
                f"fitweek:recovery-application:{user.id}:{command.client_request_id}",
            ),
            user_id=user.id,
            client_request_id=command.client_request_id,
            application_fingerprint=prepared.application_fingerprint,
            request_fingerprint=prepared.request_fingerprint,
            recovery_draft_id=prepared.draft.id,
            root_plan_id=prepared.source.series_id,
            source_revision=prepared.source.revision,
            created_revision=revision.revision if revision else None,
            applied_action_candidate_ids=command.selected_action_candidate_ids,
            session_design_draft_ids=tuple(
                sorted((item.id for item in prepared.session_design_drafts), key=str)
            ),
            schedule_draft_ids=tuple(
                sorted((item.id for item in prepared.schedule_drafts), key=str)
            ),
            affected_session_ids=tuple(
                sorted(
                    set(prepared.resolved.remove_session_ids)
                    | set(prepared.resolved.redesign_session_ids)
                    | set(prepared.resolved.reschedule_session_ids),
                    key=str,
                )
            ),
            removed_session_ids=prepared.resolved.remove_session_ids,
            preserved_session_ids=prepared.resolved.preserve_session_ids,
            immutable_session_ids=prepared.preview.immutable_session_ids,
            outcome=outcome,
            created_at=now,
        )
        applied = prepared.draft.mark_applied(
            application_result_id=result.id,
            root_plan_id=prepared.source.series_id,
            source_revision=prepared.source.revision,
            created_revision=revision.revision if revision else None,
            affected_session_ids=result.affected_session_ids,
            session_design_draft_ids=result.session_design_draft_ids,
            schedule_draft_ids=result.schedule_draft_ids,
            at=now,
        )
        applied_designs = (
            tuple(
                item.mark_applied(
                    root_plan_id=prepared.source.series_id,
                    revision=revision.revision,
                    session_id=prepared.session_design_targets[item.id],
                    application_result_id=result.id,
                    at=now,
                )
                for item in prepared.session_design_drafts
            )
            if revision is not None
            else ()
        )
        applied_schedules = (
            tuple(
                item.mark_applied(
                    root_plan_id=prepared.source.series_id,
                    source_revision=prepared.source.revision,
                    created_revision=revision.revision,
                    application_result_id=result.id,
                    at=now,
                )
                for item in prepared.schedule_drafts
            )
            if revision is not None
            else ()
        )
        try:
            commit = await self._applications.commit(
                source=prepared.source,
                expected_draft=prepared.draft,
                applied_draft=applied,
                revision=revision,
                result=result,
                applied_session_design_drafts=applied_designs,
                applied_schedule_drafts=applied_schedules,
            )
        except RepositoryUniqueError as exc:
            if exc.constraint.endswith("user_request"):
                raise RecoveryApplyIdempotencyConflict(str(exc)) from exc
            raise RecoveryPlanVersionConflict(str(exc)) from exc
        except RepositoryConflictError as exc:
            if exc.resource == "RecoveryDraft":
                raise RecoveryDraftVersionConflict(str(exc)) from exc
            raise RecoveryPlanVersionConflict(str(exc)) from exc
        except RuntimeError as exc:
            raise RecoveryPlanApplicationFailed(
                "The atomic in-memory Recovery commit failed."
            ) from exc
        return await self._outcome(user, commit.result, created=commit.created)

    async def get_result(
        self, user: UserAccount, draft_id: UUID
    ) -> RecoveryApplyOutcome:
        result = await self._applications.get_result_by_draft(user.id, draft_id)
        if result is None:
            raise RecoveryApplicationNotFound(
                "Recovery application result was not found."
            )
        return await self._outcome(user, result, created=False)

    async def verify_result(self, user: UserAccount, draft_id: UUID) -> bool:
        """Reload and re-run the complete deterministic Safety gate."""

        outcome = await self.get_result(user, draft_id)
        if outcome.plan is None:
            return True
        profile = await self._profiles.get_by_user_id(user.id)
        if profile is None:
            raise RecoveryPlanSafetyFailed("Fitness Profile was not found.")
        constraints = tuple(await self._profiles.list_constraints(profile.id))
        validation_profile = (
            replace(profile, weekly_frequency=len(outcome.plan.sessions))
            if outcome.result.removed_session_ids
            else profile
        )
        validation = self._safety.validate_plan(
            profile=validation_profile,
            constraints=constraints,
            plan=outcome.plan,
            exercise_catalog=await self._catalog(outcome.plan),
        )
        if not validation.passed:
            raise RecoveryPlanSafetyFailed(
                "The stored Recovery Plan failed deterministic Safety verification.",
                violations=validation.violations,
            )
        return True

    async def memory_preview(
        self,
        user: UserAccount,
        draft_id: UUID,
        selected_ids: tuple[UUID, ...],
    ) -> RecoveryMemoryProposalPreview:
        draft = await self._load_draft(user.id, draft_id)
        self._require_reviewed_for_memory(draft)
        proposals = await self._selected_proposals(user.id, draft, selected_ids)
        summary = await self._drafts.get_summary(user.id, draft.id)
        if summary is None:
            raise RecoveryMemoryProposalInvalid("Behavior Summary is unavailable.")
        active = [item.memory for item in await self._memories.list_memories(user)]
        now = utc_now()
        items = tuple(
            self._memory_import_policy.evaluate(
                proposal=proposal,
                summary=summary,
                active_memories=active,
                now=now,
            )
            for proposal in proposals
        )
        fingerprint = self._hash(
            {
                "draft": str(draft.id),
                "draft_version": draft.version,
                "proposals": [str(item) for item in selected_ids],
                "summary": summary.fingerprint,
                "policy": "behavior-memory-candidate-import-v1",
            }
        )
        return RecoveryMemoryProposalPreview(
            draft_id=draft.id,
            draft_version=draft.version,
            items=items,
            fingerprint=fingerprint,
        )

    async def import_memory_proposals(
        self,
        user: UserAccount,
        draft_id: UUID,
        command: ImportRecoveryMemoryProposalsCommand,
    ) -> RecoveryMemoryImportOutcome:
        preview = await self.memory_preview(
            user, draft_id, command.selected_proposal_ids
        )
        if preview.draft_version != command.expected_draft_version:
            raise RecoveryDraftVersionConflict("The Recovery Draft version is stale.")
        fingerprint = self._hash(
            {
                "user": str(user.id),
                "request": command.client_request_id,
                "preview": preview.fingerprint,
            }
        )
        existing = await self._applications.get_memory_import(
            user.id, command.client_request_id
        )
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise RecoveryMemoryProposalIdempotencyConflict(
                    "The proposal import request ID was reused with another payload."
                )
            return RecoveryMemoryImportOutcome(result=existing, created=False)
        invalid = [item for item in preview.items if not item.importable]
        if invalid:
            raise RecoveryMemoryProposalConflict(
                "One or more Behavior proposals are duplicate, conflicting, or unsafe."
            )
        proposals = await self._selected_proposals(
            user.id,
            await self._load_draft(user.id, draft_id),
            command.selected_proposal_ids,
        )
        candidate_ids: list[UUID] = []
        for proposal in proposals:
            outcome = await self._memories.create_candidate(
                user,
                CreateCandidateCommand(
                    client_request_id=(f"recovery-proposal:{draft_id}:{proposal.id}"),
                    memory_type=proposal.memory_type,
                    key=proposal.proposed_key,
                    value=proposal.proposed_value,
                    source=MemorySource.BEHAVIOR_CANDIDATE,
                    source_reference=f"recovery-draft:{draft_id}:proposal:{proposal.id}",
                    evidence_summary=(
                        f"{len(proposal.evidence_checkin_ids)} "
                        "frozen check-in references."
                    ),
                    confidence=self._confidence(proposal.confidence_tier.value),
                    expires_at=proposal.expires_at,
                ),
            )
            candidate_ids.append(outcome.candidate.id)
        result = RecoveryMemoryProposalImportResult(
            id=uuid5(
                NAMESPACE_URL,
                f"fitweek:recovery-memory-import:{user.id}:{command.client_request_id}",
            ),
            user_id=user.id,
            draft_id=draft_id,
            client_request_id=command.client_request_id,
            fingerprint=fingerprint,
            proposal_ids=tuple(sorted(command.selected_proposal_ids, key=str)),
            memory_candidate_ids=tuple(sorted(candidate_ids, key=str)),
            created_at=utc_now(),
        )
        try:
            saved = await self._applications.save_memory_import(result)
            return RecoveryMemoryImportOutcome(result=saved, created=True)
        except RepositoryUniqueError as exc:
            raise RecoveryMemoryProposalIdempotencyConflict(str(exc)) from exc

    async def create_calendar_operation_draft(
        self,
        user: UserAccount,
        result_id: UUID,
        command: CreateCalendarOperationCommand,
    ) -> tuple[CalendarOperationDraft, bool]:
        result = await self._applications.get_result(user.id, result_id)
        if result is None:
            raise RecoveryApplicationNotFound("Recovery application was not found.")
        if result.created_revision is None:
            raise RecoveryInvalidActionSelection(
                "A no-change Recovery result has no Calendar reconciliation."
            )
        plan = await self._plans.get_revision_for_user(
            result.root_plan_id, user.id, result.created_revision
        )
        if plan is None or plan.status is not WeeklyPlanStatus.CONFIRMED:
            raise RecoveryPlanVersionConflict(
                "Calendar reconciliation requires the confirmed Recovery revision."
            )
        return await self._calendar.create_draft(
            user, result.root_plan_id, result.created_revision, command
        )

    async def _prepare(
        self,
        user: UserAccount,
        draft_id: UUID,
        command: ApplyRecoveryDraftCommand,
        *,
        require_children: bool = False,
    ) -> _Prepared:
        draft = await self._load_draft(user.id, draft_id)
        self._validate_draft(draft, command)
        source = await self._load_source(user.id, command)
        candidate_set = await self._drafts.get_candidate_set(user.id, draft.id)
        impact = await self._drafts.get_impact(user.id, draft.id)
        summary = await self._drafts.get_summary(user.id, draft.id)
        if candidate_set is None or impact is None or summary is None:
            raise RecoveryInvalidActionSelection(
                "Frozen Recovery application references are unavailable."
            )
        resolved = self._resolution.resolve(
            draft=draft,
            candidates=candidate_set.candidates,
            selected_ids=command.selected_action_candidate_ids,
            plan_session_ids=tuple(item.id for item in source.sessions),
        )
        await self._validate_spacing(user, source, resolved.actions)
        check_ins = tuple(await self._check_ins.list_by_series(source.series_id))
        checked = {item.session_id for item in check_ins}
        immutable = tuple(
            sorted(
                (
                    item.id
                    for item in source.sessions
                    if item.id in checked
                    or item.status.value in {"COMPLETED", "SKIPPED", "CANCELLED"}
                    or item.scheduled_start <= utc_now()
                ),
                key=str,
            )
        )
        affected = (
            set(resolved.remove_session_ids)
            | set(resolved.redesign_session_ids)
            | set(resolved.reschedule_session_ids)
        )
        if affected & set(immutable):
            raise RecoveryTargetSessionImmutable(
                "Recovery cannot modify a checked-in, terminal, or started Session."
            )
        design_drafts, design_targets = await self._load_designs(
            draft, resolved, require_children
        )
        schedule_drafts = await self._load_schedules(draft, resolved, require_children)
        request_fingerprint = self._request_fingerprint(user.id, draft_id, command)
        application_fingerprint = self._hash(
            {
                "request": request_fingerprint,
                "draft_fingerprint": draft.request_fingerprint,
                "candidate_set": candidate_set.fingerprint,
                "behavior_summary": summary.fingerprint,
                "impact": impact.fingerprint,
                "session_design_drafts": [
                    f"{item.id}:{item.version}:{item.request_payload_fingerprint}"
                    for item in design_drafts
                ],
                "schedule_drafts": [
                    f"{item.id}:{item.version}:{item.request_fingerprint}"
                    for item in schedule_drafts
                ],
                "resolution_policy": ACTION_RESOLUTION_POLICY_VERSION,
                "application_policy": PLAN_APPLICATION_POLICY_VERSION,
            }
        )
        preview = RecoveryApplyPreview(
            draft_id=draft.id,
            draft_version=draft.version,
            root_plan_id=source.series_id,
            source_revision=source.revision,
            source_plan_version=source.version,
            selected_actions=resolved.previews,
            sessions_to_preserve=resolved.preserve_session_ids,
            sessions_to_remove=resolved.remove_session_ids,
            sessions_requiring_redesign=resolved.redesign_session_ids,
            sessions_requiring_reschedule=resolved.reschedule_session_ids,
            immutable_session_ids=immutable,
            session_design_draft_ids=tuple(
                sorted((item.id for item in design_drafts), key=str)
            ),
            schedule_draft_ids=tuple(
                sorted((item.id for item in schedule_drafts), key=str)
            ),
            requires_session_design_drafts=bool(resolved.redesign_session_ids),
            requires_schedule_draft=bool(resolved.reschedule_session_ids),
            requires_new_plan_revision=bool(affected),
            requires_calendar_reconciliation=(
                resolved.requires_calendar_reconciliation
            ),
            frequency_before=len(source.sessions),
            frequency_after=len(source.sessions) - len(resolved.remove_session_ids),
            validation=RecoveryApplyValidation(passed=True, codes=()),
            application_fingerprint=application_fingerprint,
        )
        return _Prepared(
            source=source,
            draft=draft,
            candidate_set=candidate_set,
            resolved=resolved,
            check_ins=check_ins,
            session_design_drafts=design_drafts,
            session_design_targets=design_targets,
            schedule_drafts=schedule_drafts,
            request_fingerprint=request_fingerprint,
            application_fingerprint=application_fingerprint,
            preview=preview,
        )

    async def _validate_spacing(
        self,
        user: UserAccount,
        source: WeeklyPlan,
        selected: tuple[RecoveryActionCandidate, ...],
    ) -> None:
        if self._tool_gateway is None:
            return
        now = self._tool_gateway.clock.now()
        outcome = await self._tool_gateway.invoke(
            ToolInvocationContext(
                invocation_id=uuid4(),
                correlation_id=uuid4(),
                user_id=user.id,
                caller=ToolCaller.RECOVERY_APPLICATION,
                tool_id=ToolId.RECOVERY_SPACING_VALIDATOR,
                tool_version="phase-8a-v1",
                deadline_at=now + timedelta(seconds=1),
                created_at=now,
            ),
            RecoverySpacingRequest(plan=source, selected=selected),
        )
        if (
            outcome.result.status is not ToolInvocationStatus.SUCCEEDED
            or not isinstance(outcome.response, RecoverySpacingResponse)
            or not outcome.response.passed
        ):
            raise RecoveryInvalidActionSelection(
                "The selected Recovery actions violate the spacing policy."
            )

    async def _load_draft(self, user_id: UUID, draft_id: UUID) -> RecoveryDraft:
        draft = await self._drafts.get_draft(user_id, draft_id)
        if draft is None:
            raise RecoveryDraftNotFound("Recovery Draft was not found.")
        return draft

    @staticmethod
    def _validate_draft(
        draft: RecoveryDraft, command: ApplyRecoveryDraftCommand
    ) -> None:
        if draft.status is RecoveryDraftStatus.APPLIED:
            raise RecoveryDraftAlreadyApplied("Recovery Draft was already applied.")
        if draft.status is not RecoveryDraftStatus.ACCEPTED:
            raise RecoveryDraftNotAccepted(
                "Only an ACCEPTED Recovery Draft can be applied."
            )
        if draft.version != command.expected_draft_version:
            raise RecoveryDraftVersionConflict("The Recovery Draft version is stale.")
        if draft.expires_at <= utc_now():
            raise RecoveryDraftNotAccepted("The Recovery Draft has expired.")
        if (
            draft.scope_status is not RecoveryScopeStatus.SUPPORTED
            or draft.outcome
            not in {RecoveryDraftOutcome.COMPLETE, RecoveryDraftOutcome.NO_CHANGE}
        ):
            raise RecoveryDraftNotAccepted(
                "Partial or user-action-required Recovery Drafts cannot be applied."
            )

    async def _load_source(
        self, user_id: UUID, command: ApplyRecoveryDraftCommand
    ) -> WeeklyPlan:
        source = await self._plans.get_revision_for_user(
            command.root_plan_id, user_id, command.source_revision
        )
        if source is None:
            raise RecoverySourcePlanNotFound("Recovery source Plan was not found.")
        current = await self._plans.get_current_confirmed(command.root_plan_id, user_id)
        if (
            source.status is not WeeklyPlanStatus.CONFIRMED
            or current is None
            or current.id != source.id
        ):
            raise RecoverySourceRevisionNotCurrent(
                "Only the current confirmed Revision can be recovered."
            )
        if source.version != command.expected_plan_version:
            raise RecoveryPlanVersionConflict(
                "The Recovery source Plan version is stale."
            )
        return source

    async def _load_designs(
        self,
        draft: RecoveryDraft,
        resolved: ResolvedRecoveryActions,
        required: bool,
    ) -> tuple[tuple[SessionDesignDraft, ...], dict[UUID, UUID]]:
        values: list[SessionDesignDraft] = []
        targets: dict[UUID, UUID] = {}
        for action in resolved.actions:
            if not action.requires_session_design_draft:
                continue
            child_id = await self._applications.get_session_design_subdraft(
                draft.id, action.id
            )
            child = (
                await self._session_designs.get_draft(draft.user_id, child_id)
                if child_id is not None
                else None
            )
            if child is None:
                if required:
                    raise RecoverySubdraftReviewRequired(
                        "A Session Design child Draft must be created and accepted."
                    )
                continue
            values.append(child)
            assert action.target_session_id is not None
            targets[child.id] = action.target_session_id
        return tuple(sorted(values, key=lambda item: str(item.id))), targets

    async def _load_schedules(
        self,
        draft: RecoveryDraft,
        resolved: ResolvedRecoveryActions,
        required: bool,
    ) -> tuple[ScheduleDraft, ...]:
        values: list[ScheduleDraft] = []
        for action in resolved.actions:
            if not action.requires_schedule_draft:
                continue
            child_id = await self._applications.get_schedule_subdraft(
                draft.id, action.id
            )
            child = (
                await self._schedules.get_draft(draft.user_id, child_id)
                if child_id is not None
                else None
            )
            if child is None:
                if required:
                    raise RecoverySubdraftReviewRequired(
                        "A Schedule child Draft must be created and accepted."
                    )
                continue
            values.append(child)
        return tuple(sorted(values, key=lambda item: str(item.id)))

    @staticmethod
    def _session_design_command(
        draft: RecoveryDraft,
        action: RecoveryActionCandidate,
        target: WorkoutSession,
    ) -> SessionDesignRequest:
        if action.redesign_goal is None:
            raise RecoveryInvalidActionSelection(
                "A redesign Recovery action is missing its controlled goal."
            )
        goal = {
            RecoveryRedesignGoal.LOWER_LOAD: FitnessGoal.GENERAL_FITNESS,
            RecoveryRedesignGoal.SHORTER_DURATION: FitnessGoal.GENERAL_FITNESS,
            RecoveryRedesignGoal.LOW_IMPACT: FitnessGoal.LOW_IMPACT_CARDIO,
        }[action.redesign_goal]
        return SessionDesignRequest(
            client_request_id=f"recovery:{draft.id}:{action.id}:session-design",
            target_date=target.scheduled_start.astimezone(UTC).date(),
            target_duration_minutes=target.estimated_minutes,
            location=target.location_type,
            goal=goal,
            preferred_session_type=target.session_type,
        )

    @staticmethod
    def _schedule_command(
        user: UserAccount,
        draft: RecoveryDraft,
        action: RecoveryActionCandidate,
        source: WeeklyPlan,
        target: WorkoutSession,
    ) -> CreateScheduleDraftCommand:
        timezone = ZoneInfo(user.timezone)
        local_target = target.scheduled_start.astimezone(timezone)
        duration = target.scheduled_end - target.scheduled_start
        now = utc_now()
        windows: list[AvailabilityWindow] = []
        for offset in range(7):
            day = source.week_start + timedelta(days=offset)
            start = datetime.combine(day, local_target.timetz(), timezone).astimezone(
                UTC
            )
            end = start + duration
            if start > now and start != target.scheduled_start:
                windows.append(
                    AvailabilityWindow(
                        start=start,
                        end=end,
                        location=target.location_type,
                    )
                )
        if not windows:
            windows.append(
                AvailabilityWindow(
                    start=target.scheduled_start,
                    end=target.scheduled_end,
                    location=target.location_type,
                )
            )
        return CreateScheduleDraftCommand(
            client_request_id=f"recovery:{draft.id}:{action.id}:schedule",
            root_plan_id=source.series_id,
            source_revision=source.revision,
            expected_plan_version=source.version,
            timezone=user.timezone,
            availability_windows=tuple(windows),
            target_session_ids=(target.id,),
        )

    async def _outcome(
        self,
        user: UserAccount,
        result: RecoveryApplicationResult,
        *,
        created: bool,
    ) -> RecoveryApplyOutcome:
        draft = await self._load_draft(user.id, result.recovery_draft_id)
        plan = (
            await self._plans.get_revision_for_user(
                result.root_plan_id, user.id, result.created_revision
            )
            if result.created_revision is not None
            else None
        )
        return RecoveryApplyOutcome(
            result=result, plan=plan, draft=draft, created=created
        )

    async def _catalog(self, plan: WeeklyPlan) -> dict[str, Exercise]:
        catalog = {item.id: item for item in await self._exercises.list_active()}
        for exercise_id in sorted(
            {
                exercise.exercise_id
                for session in plan.sessions
                for exercise in session.exercises
            }
        ):
            if exercise_id not in catalog:
                value = await self._exercises.get(exercise_id)
                if value is not None:
                    catalog[exercise_id] = value
        return catalog

    @staticmethod
    def _request_fingerprint(
        user_id: UUID, draft_id: UUID, command: ApplyRecoveryDraftCommand
    ) -> str:
        return RecoveryApplicationService._hash(
            {
                "user": str(user_id),
                "draft": str(draft_id),
                "expected_draft_version": command.expected_draft_version,
                "root_plan": str(command.root_plan_id),
                "source_revision": command.source_revision,
                "expected_plan_version": command.expected_plan_version,
                "actions": [
                    str(item) for item in command.selected_action_candidate_ids
                ],
                "policy": PLAN_APPLICATION_POLICY_VERSION,
            }
        )

    @staticmethod
    def _hash(value: object) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _require_reviewed_for_memory(draft: RecoveryDraft) -> None:
        if draft.status not in {
            RecoveryDraftStatus.ACCEPTED,
            RecoveryDraftStatus.APPLIED,
        }:
            raise RecoveryMemoryProposalInvalid(
                "Behavior proposals require an accepted Recovery Draft."
            )

    async def _selected_proposals(
        self,
        user_id: UUID,
        draft: RecoveryDraft,
        selected_ids: tuple[UUID, ...],
    ) -> tuple[BehaviorMemoryProposal, ...]:
        proposals = await self._drafts.list_proposals(user_id, draft.id)
        by_id = {item.id: item for item in proposals}
        if not selected_ids or any(item not in by_id for item in selected_ids):
            raise RecoveryMemoryProposalInvalid(
                "Selected Behavior proposals are outside the frozen Draft."
            )
        return tuple(by_id[item] for item in sorted(set(selected_ids), key=str))

    @staticmethod
    def _confidence(tier: str) -> Decimal:
        return {
            "HIGH": Decimal("0.9"),
            "MEDIUM": Decimal("0.7"),
            "LOW": Decimal("0.5"),
        }.get(tier, Decimal("0.5"))
