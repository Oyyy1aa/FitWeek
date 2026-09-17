"""Application orchestration for read-only analysis and Recovery Draft review."""

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from app.agents.recovery_agent import RecoveryAgent
from app.application.contexts import ContextApplicationService
from app.application.errors import (
    RecoveryDraftIdempotencyConflict,
    RecoveryDraftNotAcceptable,
    RecoveryDraftNotFound,
    RecoveryDraftReviewConflict,
    RecoveryDraftVersionConflict,
    RecoveryPlanVersionConflict,
    RecoveryScopeOutOfScope,
    RecoverySourcePlanNotFound,
    RecoverySourceRevisionNotCurrent,
    RecoveryTargetSessionImmutable,
    RecoveryTargetSessionNotFound,
)
from app.behavior.metrics import RecoveryMetrics
from app.behavior.summary_builder import BehaviorSummaryBuilder
from app.domain.behavior.models import BehaviorMemoryProposal, BehaviorSummary
from app.domain.calendar_operations.protocols import CalendarOperationRepository
from app.domain.checkins.models import WorkoutCheckIn
from app.domain.checkins.repositories import CheckInRepository
from app.domain.common import (
    DomainConflictError,
    InvalidDomainStateTransition,
    RepositoryConflictError,
    RepositoryUniqueError,
)
from app.domain.context.enums import AgentType
from app.domain.context.models import ContextBuildCommand
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.profiles.repositories import ProfileRepository
from app.domain.recovery.enums import (
    RecoveryActionType,
    RecoveryDraftOutcome,
    RecoveryDraftSource,
    RecoveryDraftStatus,
    RecoveryScopeStatus,
)
from app.domain.recovery.models import (
    CreateRecoveryDraftCommand,
    RecoveryActionCandidateSet,
    RecoveryChangeImpactSnapshot,
    RecoveryDraft,
    RecoveryTrace,
)
from app.domain.recovery.repositories import RecoveryDraftRepository
from app.domain.recovery.validation import (
    RECOVERY_AGENT_PROMPT_VERSION,
    RECOVERY_DRAFT_POLICY_VERSION,
    RECOVERY_SCOPE_POLICY_VERSION,
)
from app.domain.tools.enums import ToolCaller, ToolId, ToolInvocationStatus
from app.domain.tools.models import ToolInvocationContext
from app.domain.users.models import UserAccount
from app.orchestration.clock import Clock
from app.recovery.candidate_generator import RecoveryCandidateGenerator
from app.recovery.candidate_set import RecoveryCandidateSetBuilder
from app.recovery.change_impact import RecoveryChangeImpactAnalyzer
from app.recovery.deterministic_fallback import DeterministicRecoveryFallback
from app.recovery.memory_proposals import BehaviorMemoryProposalBuilder
from app.recovery.scope_guard import RecoveryScopeGuard
from app.tool_adapters.contracts import RecoverySpacingRequest, RecoverySpacingResponse
from app.tool_gateway.gateway import ToolGateway


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryDraftCreationResult:
    draft: RecoveryDraft
    created: bool


class RecoveryDraftService:
    """Coordinate reads, frozen analysis, provider selection, and Draft storage."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        plans: PlanRepository,
        check_ins: CheckInRepository,
        calendar_operations: CalendarOperationRepository,
        drafts: RecoveryDraftRepository,
        contexts: ContextApplicationService,
        behavior: BehaviorSummaryBuilder,
        impact: RecoveryChangeImpactAnalyzer,
        candidates: RecoveryCandidateGenerator,
        candidate_sets: RecoveryCandidateSetBuilder,
        proposals: BehaviorMemoryProposalBuilder,
        agent: RecoveryAgent,
        scope_guard: RecoveryScopeGuard,
        fallback: DeterministicRecoveryFallback,
        clock: Clock,
        metrics: RecoveryMetrics,
        enabled: bool,
        ttl_minutes: int = 30,
        tool_gateway: ToolGateway | None = None,
    ) -> None:
        self._profiles = profiles
        self._plans = plans
        self._check_ins = check_ins
        self._calendar_operations = calendar_operations
        self._drafts = drafts
        self._contexts = contexts
        self._behavior = behavior
        self._impact = impact
        self._candidates = candidates
        self._candidate_sets = candidate_sets
        self._proposals = proposals
        self._agent = agent
        self._scope = scope_guard
        self._fallback = fallback
        self._clock = clock
        self._metrics = metrics
        self._enabled = enabled
        self._ttl_minutes = ttl_minutes
        self._tool_gateway = tool_gateway

    def attach_tool_gateway(self, gateway: ToolGateway) -> None:
        self._tool_gateway = gateway

    async def create(
        self, user: UserAccount, command: CreateRecoveryDraftCommand
    ) -> RecoveryDraftCreationResult:
        self._metrics.recovery_draft_requests += 1
        started = time.perf_counter()
        scope = self._scope.evaluate(command.user_request)
        if scope.status is RecoveryScopeStatus.OUT_OF_SCOPE:
            self._metrics.recovery_scope_blocks += 1
            raise RecoveryScopeOutOfScope(scope.safe_message)
        if scope.status is RecoveryScopeStatus.NEEDS_REVIEW:
            self._metrics.recovery_scope_reviews += 1

        profile = await self._profiles.get_by_user_id(user.id)
        if profile is None:
            raise RecoverySourcePlanNotFound("A Fitness Profile is required.")
        constraints = tuple(await self._profiles.list_constraints(profile.id))
        source = await self._plans.get_revision_for_user(
            command.root_plan_id, user.id, command.source_revision
        )
        if source is None:
            raise RecoverySourcePlanNotFound("The source Plan was not found.")
        current = await self._plans.get_current_confirmed(command.root_plan_id, user.id)
        if (
            current is None
            or source.id != current.id
            or source.status is not WeeklyPlanStatus.CONFIRMED
        ):
            raise RecoverySourceRevisionNotCurrent(
                "Recovery can only inspect the current confirmed Plan revision."
            )
        if current.version != command.expected_plan_version:
            raise RecoveryPlanVersionConflict("The source Plan version changed.")
        behavior_plans = tuple(await self._plans.list_by_user(user.id))
        behavior_check_ins = tuple(await self._check_ins.list_by_user(user.id))
        current_check_ins = tuple(
            await self._check_ins.list_by_series(command.root_plan_id)
        )
        payload_fingerprint = self._payload_fingerprint(
            user_id=user.id,
            command=command,
            profile_version=profile.version,
            constraint_versions=tuple((item.id, item.version) for item in constraints),
            plan=current,
            check_ins=behavior_check_ins,
        )
        existing = await self._drafts.get_by_request(user.id, command.client_request_id)
        if existing is not None:
            if existing.request_payload_fingerprint != payload_fingerprint:
                self._metrics.recovery_idempotency_conflicts += 1
                raise RecoveryDraftIdempotencyConflict(
                    "The Recovery request key was already used for different evidence."
                )
            self._metrics.recovery_idempotent_reuses += 1
            return RecoveryDraftCreationResult(
                draft=await self._expire_if_needed(existing), created=False
            )

        summary = self._behavior.build(
            user_id=user.id,
            timezone=user.timezone,
            plans=behavior_plans,
            check_ins=behavior_check_ins,
            window=command.behavior_window,
        )
        bindings = await self._calendar_operations.list_bindings_for_plan(
            user.id, command.root_plan_id
        )
        impact = self._impact.analyze(
            user_id=user.id,
            plan=current,
            check_ins=current_check_ins,
            calendar_bindings=bindings,
            request_type=command.request_type,
            target_session_ids=command.target_session_ids,
        )
        self._validate_targets(command, current, impact)
        candidates = (
            ()
            if scope.status is RecoveryScopeStatus.NEEDS_REVIEW
            else self._candidates.generate(
                request_type=command.request_type,
                target_session_ids=command.target_session_ids,
                plan=current,
                impact=impact,
                behavior=summary,
            )
        )
        behavior_items = self._behavior_context_items(summary)
        context_command = ContextBuildCommand(
            agent_type=AgentType.RECOVERY_AGENT,
            current_task={
                "request_type": command.request_type.value,
                "source_revision": str(command.source_revision),
                "target_session_ids": ",".join(
                    str(item) for item in command.target_session_ids or ()
                )
                or "AUTO",
                "mutable_session_ids": ",".join(
                    str(item) for item in impact.mutable_session_ids
                )
                or "NONE",
                "candidate_count": str(len(candidates)),
            },
            recent_behavior_summary=behavior_items,
            plan_id=current.series_id,
        )
        context_scope_fingerprint = self._context_scope_fingerprint(
            user_id=user.id,
            command=context_command,
            profile_version=profile.version,
            constraint_versions=tuple((item.id, item.version) for item in constraints),
            summary_fingerprint=summary.fingerprint,
            impact_fingerprint=impact.fingerprint,
        )
        context = await self._contexts.build_snapshot(
            user,
            context_command,
            scope_id=f"recovery:{context_scope_fingerprint}",
        )
        candidate_set = self._candidate_sets.build(
            user_id=user.id,
            command=command,
            profile_version=profile.version,
            constraint_versions=tuple(
                sorted(
                    ((item.id, item.version) for item in constraints),
                    key=lambda item: str(item[0]),
                )
            ),
            summary=summary,
            context=context.reference,
            impact=impact,
            candidates=candidates,
        )
        memory_proposals = self._proposals.build(
            user_id=user.id,
            summary=summary,
            user_request=command.user_request,
            proposal_scope=command.client_request_id,
        )
        request_fingerprint = self._combined_fingerprint(
            payload_fingerprint,
            summary.fingerprint,
            context.reference.context_fingerprint,
            impact.fingerprint,
            candidate_set.fingerprint,
            RECOVERY_SCOPE_POLICY_VERSION,
            RECOVERY_DRAFT_POLICY_VERSION,
            RECOVERY_AGENT_PROMPT_VERSION,
        )
        request_id = uuid4()
        if scope.status is RecoveryScopeStatus.NEEDS_REVIEW:
            selected_ids: tuple[UUID, ...] = ()
            unresolved = command.target_session_ids or impact.mutable_session_ids
            explanation = scope.safe_message
            outcome = RecoveryDraftOutcome.USER_ACTION_REQUIRED
            source_kind = RecoveryDraftSource.SCOPE_GUARD
            fallback_used = False
            provider_name = "scope-guard"
            provider_version = RECOVERY_SCOPE_POLICY_VERSION
            attempts = 0
        else:
            if self._enabled and candidate_set.candidates:
                agent_result = await self._agent.run(
                    user_id=user.id,
                    request_fingerprint=request_fingerprint,
                    request_type=command.request_type,
                    candidate_set=candidate_set,
                    behavior_summary=summary,
                    impact=impact,
                    plan=current,
                    context=context,
                    request_id=request_id,
                )
                output = agent_result.output
                fallback_used = agent_result.fallback_used
                provider_name = agent_result.provider_name
                provider_version = agent_result.provider_version
                attempts = agent_result.provider_attempts
            else:
                output = self._fallback.build(
                    candidate_set,
                    request_type=command.request_type,
                    impact=impact,
                )
                fallback_used = True
                provider_name = "deterministic-template"
                provider_version = RECOVERY_DRAFT_POLICY_VERSION
                attempts = 0
            selected_ids = output.selected_action_candidate_ids
            if self._tool_gateway is not None:
                selected = tuple(
                    candidate_set.candidate_map[item] for item in selected_ids
                )
                tool_now = self._tool_gateway.clock.now()
                spacing = await self._tool_gateway.invoke(
                    ToolInvocationContext(
                        invocation_id=uuid4(),
                        correlation_id=uuid4(),
                        user_id=user.id,
                        caller=ToolCaller.RECOVERY_APPLICATION,
                        tool_id=ToolId.RECOVERY_SPACING_VALIDATOR,
                        tool_version="phase-8a-v1",
                        deadline_at=tool_now + timedelta(seconds=1),
                        created_at=tool_now,
                    ),
                    RecoverySpacingRequest(plan=current, selected=selected),
                )
                if (
                    spacing.result.status is not ToolInvocationStatus.SUCCEEDED
                    or not isinstance(spacing.response, RecoverySpacingResponse)
                    or not spacing.response.passed
                ):
                    output = self._fallback.build(
                        candidate_set,
                        request_type=command.request_type,
                        impact=impact,
                    )
                    selected_ids = output.selected_action_candidate_ids
            unresolved = output.unresolved_session_ids
            explanation = output.explanation_summary
            outcome = self._outcome(candidate_set, selected_ids, unresolved)
            source_kind = (
                RecoveryDraftSource.DETERMINISTIC_FALLBACK
                if fallback_used
                else RecoveryDraftSource.MODEL
            )
            if fallback_used:
                self._metrics.recovery_deterministic_fallbacks += 1
            elif "backup" in provider_name.casefold():
                self._metrics.recovery_backup_successes += 1
            else:
                self._metrics.recovery_primary_successes += 1

        now = self._clock.now()
        draft_id = uuid4()
        draft = RecoveryDraft(
            id=draft_id,
            request_id=request_id,
            client_request_id=command.client_request_id,
            user_id=user.id,
            request_payload_fingerprint=payload_fingerprint,
            request_fingerprint=request_fingerprint,
            root_plan_id=current.series_id,
            source_revision=current.revision,
            source_plan_version=current.version,
            behavior_summary_id=summary.id,
            context_snapshot_reference_id=context.reference.id,
            change_impact_snapshot_id=impact.id,
            candidate_set_id=candidate_set.id,
            selected_action_candidate_ids=selected_ids,
            unresolved_session_ids=unresolved,
            behavior_memory_proposal_ids=tuple(item.id for item in memory_proposals),
            explanation_summary=explanation,
            outcome=outcome,
            source=source_kind,
            fallback_used=fallback_used,
            scope_status=scope.status,
            status=RecoveryDraftStatus.PENDING_REVIEW,
            created_at=now,
            expires_at=now + timedelta(minutes=self._ttl_minutes),
            version=1,
        )
        trace = RecoveryTrace(
            id=uuid4(),
            user_id=user.id,
            draft_id=draft_id,
            request_id=request_id,
            behavior_summary_id=summary.id,
            behavior_summary_fingerprint=summary.fingerprint,
            context_snapshot_reference_id=context.reference.id,
            change_impact_snapshot_id=impact.id,
            candidate_set_id=candidate_set.id,
            candidate_set_fingerprint=candidate_set.fingerprint,
            scope_status=scope.status,
            provider_name=provider_name,
            provider_version=provider_version,
            attempt_no=attempts,
            outcome=outcome,
            fallback_used=fallback_used,
            validation_error_code=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            created_at=now,
        )
        try:
            saved = await self._drafts.save_bundle(
                draft=draft,
                behavior_summary=summary,
                impact=impact,
                candidate_set=candidate_set,
                proposals=memory_proposals,
                trace=trace,
            )
        except RepositoryUniqueError as exc:
            concurrent = await self._drafts.get_by_request(
                user.id, command.client_request_id
            )
            if concurrent is None:
                raise
            if concurrent.request_payload_fingerprint != payload_fingerprint:
                self._metrics.recovery_idempotency_conflicts += 1
                raise RecoveryDraftIdempotencyConflict(
                    "The Recovery request key was concurrently used for "
                    "different evidence."
                ) from exc
            self._metrics.recovery_idempotent_reuses += 1
            return RecoveryDraftCreationResult(
                draft=await self._expire_if_needed(concurrent), created=False
            )
        self._increment_outcome(outcome)
        return RecoveryDraftCreationResult(draft=saved, created=True)

    async def get(self, user: UserAccount, draft_id: UUID) -> RecoveryDraft:
        draft = await self._drafts.get_draft(user.id, draft_id)
        if draft is None:
            raise RecoveryDraftNotFound("Recovery Draft was not found.")
        return await self._expire_if_needed(draft)

    async def get_summary(self, user: UserAccount, draft_id: UUID) -> BehaviorSummary:
        await self.get(user, draft_id)
        value = await self._drafts.get_summary(user.id, draft_id)
        if value is None:
            raise RecoveryDraftNotFound("Recovery behavior summary was not found.")
        return value

    async def get_impact(
        self, user: UserAccount, draft_id: UUID
    ) -> RecoveryChangeImpactSnapshot:
        await self.get(user, draft_id)
        value = await self._drafts.get_impact(user.id, draft_id)
        if value is None:
            raise RecoveryDraftNotFound("Recovery Change Impact was not found.")
        return value

    async def get_candidate_set(
        self, user: UserAccount, draft_id: UUID
    ) -> RecoveryActionCandidateSet:
        await self.get(user, draft_id)
        value = await self._drafts.get_candidate_set(user.id, draft_id)
        if value is None:
            raise RecoveryDraftNotFound("Recovery Candidate Set was not found.")
        return value

    async def get_trace(self, user: UserAccount, draft_id: UUID) -> RecoveryTrace:
        await self.get(user, draft_id)
        value = await self._drafts.get_trace(user.id, draft_id)
        if value is None:
            raise RecoveryDraftNotFound("Recovery Trace was not found.")
        return value

    async def list_proposals(
        self, user: UserAccount, draft_id: UUID
    ) -> tuple[BehaviorMemoryProposal, ...]:
        await self.get(user, draft_id)
        return await self._drafts.list_proposals(user.id, draft_id)

    async def accept(
        self, user: UserAccount, draft_id: UUID, *, expected_version: int
    ) -> RecoveryDraft:
        draft = await self.get(user, draft_id)
        if draft.outcome not in {
            RecoveryDraftOutcome.COMPLETE,
            RecoveryDraftOutcome.NO_CHANGE,
        }:
            raise RecoveryDraftNotAcceptable(
                "PARTIAL and USER_ACTION_REQUIRED Recovery Drafts cannot be accepted."
            )
        try:
            updated = draft.accept(
                expected_version=expected_version, at=self._clock.now()
            )
            saved = await self._drafts.update_draft(updated)
        except DomainConflictError as exc:
            raise RecoveryDraftVersionConflict(str(exc)) from exc
        except (InvalidDomainStateTransition, RepositoryConflictError) as exc:
            raise RecoveryDraftReviewConflict(str(exc)) from exc
        self._metrics.recovery_drafts_accepted += 1
        return saved

    async def reject(
        self, user: UserAccount, draft_id: UUID, *, expected_version: int
    ) -> RecoveryDraft:
        draft = await self.get(user, draft_id)
        try:
            updated = draft.reject(
                expected_version=expected_version, at=self._clock.now()
            )
            saved = await self._drafts.update_draft(updated)
        except DomainConflictError as exc:
            raise RecoveryDraftVersionConflict(str(exc)) from exc
        except (InvalidDomainStateTransition, RepositoryConflictError) as exc:
            raise RecoveryDraftReviewConflict(str(exc)) from exc
        self._metrics.recovery_drafts_rejected += 1
        return saved

    def metrics(self) -> dict[str, int]:
        return self._metrics.snapshot()

    async def _expire_if_needed(self, draft: RecoveryDraft) -> RecoveryDraft:
        now = self._clock.now()
        if (
            draft.status is RecoveryDraftStatus.PENDING_REVIEW
            and now >= draft.expires_at
        ):
            try:
                return await self._drafts.update_draft(draft.expire(now))
            except RepositoryConflictError:
                current = await self._drafts.get_draft(draft.user_id, draft.id)
                return current or draft
        return draft

    @staticmethod
    def _validate_targets(
        command: CreateRecoveryDraftCommand,
        plan: WeeklyPlan,
        impact: RecoveryChangeImpactSnapshot,
    ) -> None:
        if command.target_session_ids is None:
            return
        session_ids = {item.id for item in plan.sessions}
        missing = set(command.target_session_ids) - session_ids
        if missing:
            raise RecoveryTargetSessionNotFound("A target Session was not found.")
        immutable = set(command.target_session_ids) & set(impact.immutable_session_ids)
        if immutable:
            raise RecoveryTargetSessionImmutable(
                "Completed, started, cancelled, or checked-in Sessions are immutable."
            )

    @staticmethod
    def _behavior_context_items(summary: BehaviorSummary) -> tuple[str, ...]:
        values = [
            f"completed_count={summary.completed_count}",
            f"partially_completed_count={summary.partially_completed_count}",
            f"skipped_count={summary.skipped_count}",
            f"missing_checkin_count={summary.missing_checkin_count}",
        ]
        values.extend(
            f"pattern={item.pattern_id}:{item.pattern_type.value}:{item.key}"
            for item in sorted(
                {
                    item.pattern_id: item
                    for group in (
                        summary.repeated_time_patterns,
                        summary.repeated_location_patterns,
                        summary.repeated_skip_patterns,
                    )
                    for item in group
                }.values(),
                key=lambda item: item.pattern_id,
            )
        )
        return tuple(values[:20])

    @staticmethod
    def _outcome(
        candidate_set: RecoveryActionCandidateSet,
        selected_ids: tuple[UUID, ...],
        unresolved: tuple[UUID, ...],
    ) -> RecoveryDraftOutcome:
        if unresolved:
            return RecoveryDraftOutcome.PARTIAL
        selected = [candidate_set.candidate_map[item] for item in selected_ids]
        if not selected or all(
            item.action_type is RecoveryActionType.KEEP_CURRENT_PLAN
            for item in selected
        ):
            return RecoveryDraftOutcome.NO_CHANGE
        return RecoveryDraftOutcome.COMPLETE

    def _increment_outcome(self, outcome: RecoveryDraftOutcome) -> None:
        attribute = {
            RecoveryDraftOutcome.COMPLETE: "recovery_complete_drafts",
            RecoveryDraftOutcome.PARTIAL: "recovery_partial_drafts",
            RecoveryDraftOutcome.NO_CHANGE: "recovery_no_change_drafts",
            RecoveryDraftOutcome.USER_ACTION_REQUIRED: (
                "recovery_action_required_drafts"
            ),
        }[outcome]
        setattr(self._metrics, attribute, getattr(self._metrics, attribute) + 1)

    @staticmethod
    def _context_scope_fingerprint(
        *,
        user_id: UUID,
        command: ContextBuildCommand,
        profile_version: int,
        constraint_versions: tuple[tuple[UUID, int], ...],
        summary_fingerprint: str,
        impact_fingerprint: str,
    ) -> str:
        payload = {
            "user_id": str(user_id),
            "agent_type": command.agent_type.value,
            "current_task": dict(command.current_task),
            "recent_behavior_summary": list(command.recent_behavior_summary),
            "catalog_reference": command.catalog_reference,
            "max_characters": command.max_characters,
            "run_id": None if command.run_id is None else str(command.run_id),
            "step_id": None if command.step_id is None else str(command.step_id),
            "profile_draft_id": (
                None
                if command.profile_draft_id is None
                else str(command.profile_draft_id)
            ),
            "plan_id": None if command.plan_id is None else str(command.plan_id),
            "profile_version": profile_version,
            "constraint_versions": [
                (str(item), version)
                for item, version in sorted(
                    constraint_versions,
                    key=lambda value: str(value[0]),
                )
            ],
            "summary_fingerprint": summary_fingerprint,
            "impact_fingerprint": impact_fingerprint,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _payload_fingerprint(
        *,
        user_id: UUID,
        command: CreateRecoveryDraftCommand,
        profile_version: int,
        constraint_versions: tuple[tuple[UUID, int], ...],
        plan: WeeklyPlan,
        check_ins: tuple[WorkoutCheckIn, ...],
    ) -> str:
        payload = {
            "user_id": str(user_id),
            "client_request_id": command.client_request_id,
            "root_plan_id": str(command.root_plan_id),
            "source_revision": command.source_revision,
            "expected_plan_version": command.expected_plan_version,
            "request_type": command.request_type.value,
            "target_session_ids": [
                str(item) for item in command.target_session_ids or ()
            ],
            "user_request_sha256": hashlib.sha256(
                command.user_request.encode()
            ).hexdigest(),
            "behavior_window": (
                {
                    "start": command.behavior_window.start_date.isoformat()
                    if command.behavior_window and command.behavior_window.start_date
                    else None,
                    "end": command.behavior_window.end_date.isoformat()
                    if command.behavior_window and command.behavior_window.end_date
                    else None,
                }
            ),
            "profile_version": profile_version,
            "constraint_versions": [
                (str(item), version)
                for item, version in sorted(
                    constraint_versions, key=lambda value: str(value[0])
                )
            ],
            "plan_id": str(plan.id),
            "plan_version": plan.version,
            "check_ins": [
                {
                    "id": str(item.id),
                    "session_id": str(item.session_id),
                    "status": item.status.value,
                    "rpe": item.perceived_effort,
                    "occurred_at": item.occurred_at.isoformat(),
                    "version": item.version,
                }
                for item in sorted(check_ins, key=lambda value: str(value.id))
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _combined_fingerprint(*values: str) -> str:
        return hashlib.sha256(":".join(values).encode()).hexdigest()
