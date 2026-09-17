"""Application orchestration for review-only controlled Session Drafts."""

import hashlib
import json
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.agents.session_designer import SessionDesignerAgent, SessionDesignerAgentResult
from app.application.contexts import ContextApplicationService
from app.application.errors import (
    BusinessRuleViolation,
    ResourceNotFound,
    SessionDesignIdempotencyConflict,
    SessionDesignNotFound,
    SessionDesignReviewConflict,
    SessionDesignUnavailable,
)
from app.domain.common import DomainValidationError, utc_now
from app.domain.context.enums import AgentType
from app.domain.context.models import ContextBuildCommand
from app.domain.exercises.models import Exercise
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.profiles.repositories import ProfileRepository
from app.domain.session_design.enums import SessionDesignSource
from app.domain.session_design.models import (
    SessionDesignDraft,
    SessionDesignRequest,
    SessionDesignTrace,
)
from app.domain.session_design.repositories import SessionDesignRepository
from app.domain.sessions.models import SessionExercise
from app.domain.tools.enums import ToolCaller, ToolId, ToolInvocationStatus
from app.domain.tools.models import ToolInvocationContext
from app.domain.users.models import UserAccount
from app.safety.engine import SafetyEngine
from app.session_design.candidate_search import ExerciseCandidateSearcher
from app.session_design.candidate_set import build_candidate_set, catalog_version
from app.session_design.duration import SessionDurationPolicy
from app.session_design.fallback import DeterministicSessionFallback
from app.session_design.metrics import SessionDesignMetrics
from app.session_design.template_registry import SessionTemplateRegistry
from app.session_design.validator import SessionDesignerBusinessValidator
from app.tool_adapters.contracts import (
    ExerciseCatalogSearchRequest,
    ExerciseCatalogSearchResponse,
    SessionDurationRequest,
    SessionDurationResponse,
)
from app.tool_gateway.gateway import ToolGateway


class SessionDesignService:
    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        exercises: ExerciseRepository,
        drafts: SessionDesignRepository,
        contexts: ContextApplicationService,
        agent: SessionDesignerAgent,
        safety: SafetyEngine,
        metrics: SessionDesignMetrics,
        enabled: bool = True,
        tool_gateway: ToolGateway | None = None,
    ) -> None:
        self._profiles = profiles
        self._exercises = exercises
        self._drafts = drafts
        self._contexts = contexts
        self._agent = agent
        self._safety = safety
        self._metrics = metrics
        self._enabled = enabled
        self._tool_gateway = tool_gateway
        self._templates = SessionTemplateRegistry()
        self._searcher = ExerciseCandidateSearcher()
        self._duration = SessionDurationPolicy()
        self._fallback = DeterministicSessionFallback()
        self._validator = SessionDesignerBusinessValidator()

    def attach_tool_gateway(self, gateway: ToolGateway) -> None:
        self._tool_gateway = gateway

    async def create(
        self, user: UserAccount, command: SessionDesignRequest
    ) -> tuple[SessionDesignDraft, bool]:
        tool_correlation_id = uuid4()
        request_fingerprint = self._payload_fingerprint(command)
        self._metrics.requests_total += 1
        profile = await self._profiles.get_by_user_id(user.id)
        if profile is None:
            raise ResourceNotFound(
                "Create a Fitness Profile before designing a Session."
            )
        if not profile.scope_confirmed:
            raise BusinessRuleViolation(
                "The supported product scope must be confirmed.",
                code="SCOPE_NOT_CONFIRMED",
            )
        if command.target_duration_minutes > profile.max_session_minutes:
            raise SessionDesignUnavailable(
                "The requested duration exceeds the Profile limit."
            )
        constraints = tuple(await self._profiles.list_constraints(profile.id))
        catalog = await self._load_catalog(user, tool_correlation_id)
        try:
            template = self._templates.select(
                goal=command.goal,
                location=command.location,
                preferred_type=command.preferred_session_type,
                requested=command.template_id,
                experience_level=profile.experience_level,
                target_duration_minutes=command.target_duration_minutes,
            )
        except ValueError as exc:
            raise SessionDesignUnavailable(
                "No compatible controlled Session Template is available."
            ) from exc
        constraint_versions = ",".join(
            f"{item.id}:{item.version}"
            for item in sorted(constraints, key=lambda item: str(item.id))
        )
        frozen_input_fingerprint = self._combined_fingerprint(
            request_fingerprint,
            str(user.id),
            f"{profile.id}:{profile.version}",
            constraint_versions,
            catalog_version(catalog),
            template.version,
            "session-designer-v1",
            "session-design-policy-v1",
        )
        context = await self._contexts.build_snapshot(
            user,
            ContextBuildCommand(
                agent_type=AgentType.SESSION_DESIGNER,
                current_task={
                    "target_date": command.target_date.isoformat(),
                    "target_duration_minutes": str(command.target_duration_minutes),
                    "location": command.location.value,
                    "goal": command.goal.value,
                    "template_id": template.id.value,
                },
                catalog_reference="controlled-exercise-catalog",
            ),
            scope_id=self._context_scope_id(
                user.id,
                command.client_request_id,
                frozen_input_fingerprint,
            ),
        )
        try:
            slots = self._searcher.search(
                profile=profile,
                constraints=constraints,
                catalog=catalog,
                template=template,
                location=command.location,
                target_date=command.target_date,
            )
        except DomainValidationError as exc:
            self._metrics.validation_failures += 1
            raise SessionDesignUnavailable(
                "No safe controlled exercise candidates satisfy every template slot."
            ) from exc
        candidate_set = build_candidate_set(
            user_id=user.id,
            request_fingerprint=frozen_input_fingerprint,
            template=template,
            catalog=catalog,
            context=context.reference,
            slots=slots,
        )
        input_fingerprint = self._combined_fingerprint(
            frozen_input_fingerprint,
            candidate_set.fingerprint,
            context.reference.context_fingerprint,
        )
        existing = await self._drafts.get_by_request(user.id, command.client_request_id)
        if existing is not None:
            if existing.request_payload_fingerprint != input_fingerprint:
                raise SessionDesignIdempotencyConflict(
                    "The client request ID was already used with different "
                    "frozen inputs."
                )
            self._metrics.idempotent_reuses += 1
            return existing, True
        await self._drafts.save_candidate_set(candidate_set)
        catalog_map = {item.id: item for item in catalog}
        if self._enabled:
            result = await self._agent.run(
                user_id=user.id,
                request_fingerprint=input_fingerprint,
                candidate_set=candidate_set,
                template=template,
                context=context,
                profile=profile,
                constraints=constraints,
                catalog=catalog_map,
            )
        else:
            try:
                fallback_output = self._fallback.build(candidate_set, template)
            except ValueError as exc:
                raise SessionDesignUnavailable(
                    "The deterministic template fallback cannot satisfy the request."
                ) from exc
            result = SessionDesignerAgentResult(
                request_id=uuid4(),
                output=fallback_output,
                prompt_version="session-designer-v1",
                provider_summary="gateway-disabled:template-fallback",
                fallback_used=True,
                provider_attempts=0,
            )
        output = self._validator.validate(
            result.output,
            candidate_set=candidate_set,
            template=template,
            profile=profile,
            constraints=constraints,
            catalog=catalog_map,
        )
        base_exercises = tuple(
            SessionExercise(
                exercise_id=item.exercise_id,
                sequence_no=index,
                sets=item.sets,
                repetitions=item.repetitions,
                duration_seconds=item.duration_seconds,
                rest_seconds=item.rest_seconds,
            )
            for index, item in enumerate(output.selections, start=1)
        )
        if self._tool_gateway is None:
            fitted, duration = self._duration.fit_exact(
                base_exercises, command.target_duration_minutes
            )
        else:
            tool_now = self._tool_gateway.clock.now()
            duration_outcome = await self._tool_gateway.invoke(
                ToolInvocationContext(
                    invocation_id=uuid4(),
                    correlation_id=tool_correlation_id,
                    user_id=user.id,
                    caller=ToolCaller.SESSION_DESIGN_APPLICATION,
                    tool_id=ToolId.SESSION_DURATION_CALCULATOR,
                    tool_version="phase-8a-v1",
                    deadline_at=tool_now + timedelta(seconds=1),
                    created_at=tool_now,
                ),
                SessionDurationRequest(
                    exercises=base_exercises,
                    target_duration_minutes=command.target_duration_minutes,
                ),
            )
            if (
                duration_outcome.result.status is not ToolInvocationStatus.SUCCEEDED
                or not isinstance(duration_outcome.response, SessionDurationResponse)
            ):
                raise SessionDesignUnavailable(
                    "The deterministic duration policy is unavailable."
                )
            fitted = duration_outcome.response.exercises
            duration = duration_outcome.response.duration
        safety = self._safety.validate_session_design(
            profile=profile,
            constraints=constraints,
            target_date=command.target_date,
            target_duration_minutes=command.target_duration_minutes,
            location=command.location,
            exercises=fitted,
            exercise_catalog=catalog_map,
        )
        if not safety.passed:
            self._metrics.validation_failures += 1
            raise BusinessRuleViolation(
                "The Session Draft failed the final deterministic Safety gate.",
                code="SESSION_DESIGN_SAFETY_FAILED",
                violations=safety.violations,
            )
        now = utc_now()
        draft_id = uuid5(
            NAMESPACE_URL,
            f"fitweek:session-draft:{user.id}:{command.client_request_id}:{input_fingerprint}",
        )
        source = (
            SessionDesignSource.TEMPLATE_FALLBACK
            if result.fallback_used
            else SessionDesignSource.MODEL
        )
        draft = SessionDesignDraft(
            id=draft_id,
            request_id=result.request_id,
            client_request_id=command.client_request_id,
            user_id=user.id,
            request_payload_fingerprint=input_fingerprint,
            candidate_set_id=candidate_set.id,
            candidate_set_fingerprint=candidate_set.fingerprint,
            context_snapshot_reference_id=context.reference.id,
            context_fingerprint=context.reference.context_fingerprint,
            context_degraded_mode=context.reference.degraded_mode,
            template_id=template.id,
            template_version=template.version,
            catalog_version=candidate_set.catalog_version,
            session_type=template.session_type,
            target_date=command.target_date,
            target_duration_minutes=command.target_duration_minutes,
            location=command.location,
            goal=command.goal,
            exercises=fitted,
            exercise_roles=tuple(slot.role for slot in candidate_set.slots),
            duration=duration,
            safety_validation=safety,
            source=source,
            prompt_version=result.prompt_version,
            provider_summary=result.provider_summary,
            fallback_used=result.fallback_used,
            explanation_summary=output.explanation_summary,
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
        model_traces = (
            await self._agent.list_traces(user_id=user.id, request_id=result.request_id)
            if self._enabled
            else ()
        )
        trace = SessionDesignTrace(
            draft_id=draft.id,
            request_id=draft.request_id,
            candidate_set_id=candidate_set.id,
            candidate_set_fingerprint=candidate_set.fingerprint,
            context_snapshot_reference_id=context.reference.id,
            context_fingerprint=context.reference.context_fingerprint,
            prompt_version=result.prompt_version,
            template_id=template.id,
            template_version=template.version,
            provider_summary=result.provider_summary,
            source=source,
            fallback_used=result.fallback_used,
            validation_error_code=None,
            model_trace_ids=tuple(item.id for item in model_traces),
        )
        saved = await self._drafts.save_draft(draft, trace)
        if source is SessionDesignSource.MODEL:
            self._metrics.model_drafts += 1
        else:
            self._metrics.template_fallbacks += 1
        return saved, False

    async def get(self, user: UserAccount, draft_id: UUID) -> SessionDesignDraft:
        draft = await self._drafts.get_draft(user.id, draft_id)
        if draft is None:
            raise SessionDesignNotFound("Session Design Draft was not found.")
        if draft.status.value == "PENDING_REVIEW" and draft.expires_at <= utc_now():
            draft = await self._drafts.update_draft(draft.expire(utc_now()))
        return draft

    async def trace(self, user: UserAccount, draft_id: UUID) -> SessionDesignTrace:
        await self.get(user, draft_id)
        trace = await self._drafts.get_trace(user.id, draft_id)
        if trace is None:
            raise SessionDesignNotFound("Session Design trace was not found.")
        return trace

    async def review(
        self, user: UserAccount, draft_id: UUID, *, expected_version: int, accept: bool
    ) -> SessionDesignDraft:
        draft = await self.get(user, draft_id)
        if draft.version != expected_version or draft.status.value != "PENDING_REVIEW":
            raise SessionDesignReviewConflict("The Draft version or state changed.")
        reviewed = draft.accept(utc_now()) if accept else draft.reject(utc_now())
        saved = await self._drafts.update_draft(reviewed)
        if accept:
            self._metrics.accepted += 1
        else:
            self._metrics.rejected += 1
        return saved

    def metrics(self) -> dict[str, int]:
        return self._metrics.snapshot().as_dict()

    async def _load_catalog(
        self, user: UserAccount, correlation_id: UUID
    ) -> tuple[Exercise, ...]:
        if self._tool_gateway is None:
            return tuple(await self._exercises.list_active())
        now = self._tool_gateway.clock.now()
        outcome = await self._tool_gateway.invoke(
            ToolInvocationContext(
                invocation_id=uuid4(),
                correlation_id=correlation_id,
                user_id=user.id,
                caller=ToolCaller.SESSION_DESIGN_APPLICATION,
                tool_id=ToolId.EXERCISE_CATALOG_SEARCH,
                tool_version="phase-8a-v1",
                deadline_at=now + timedelta(seconds=2),
                created_at=now,
            ),
            ExerciseCatalogSearchRequest(user_id=user.id),
        )
        if (
            outcome.result.status is not ToolInvocationStatus.SUCCEEDED
            or not isinstance(outcome.response, ExerciseCatalogSearchResponse)
        ):
            raise SessionDesignUnavailable(
                "The controlled exercise catalog is unavailable."
            )
        return tuple(item.to_domain() for item in outcome.response.exercises)

    @staticmethod
    def _payload_fingerprint(command: SessionDesignRequest) -> str:
        payload = {
            "client_request_id": command.client_request_id,
            "target_date": command.target_date.isoformat(),
            "duration": command.target_duration_minutes,
            "location": command.location.value,
            "goal": command.goal.value,
            "session_type": command.preferred_session_type.value
            if command.preferred_session_type
            else None,
            "template_id": command.template_id.value if command.template_id else None,
            "policy": "session-design-policy-v1",
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _combined_fingerprint(*values: str) -> str:
        return hashlib.sha256(":".join(values).encode()).hexdigest()

    @staticmethod
    def _context_scope_id(
        user_id: UUID,
        client_request_id: str,
        frozen_input_fingerprint: str,
    ) -> str:
        identity = hashlib.sha256(
            f"{user_id}:{client_request_id}:{frozen_input_fingerprint}".encode()
        ).hexdigest()
        return f"session-design:{identity}"
