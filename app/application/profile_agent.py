"""Profile Agent draft use cases with user-scoped idempotency and no apply path."""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID, uuid4

from app.agents.profile_agent import ProfileAgent, ProfileAgentOutOfScopeError
from app.application.contexts import ContextApplicationService
from app.application.errors import (
    ModelGatewayUnavailable,
    ProfileAgentIdempotencyConflict,
    ProfileRequestOutOfScope,
    ResourceNotFound,
)
from app.domain.common import RepositoryUniqueError, utc_now
from app.domain.context.enums import (
    AgentType,
    ContextDegradedMode,
    ContextSectionName,
)
from app.domain.context.models import BuiltContext, ContextBuildCommand
from app.domain.model_gateway.models import ModelCallTrace
from app.domain.profile_agent.models import (
    ProfileAgentDraft,
    ProfileAgentInput,
    ProfileDraftStatus,
    UserProfileSnapshot,
)
from app.domain.profile_agent.repositories import ProfileAgentDraftRepository
from app.domain.profiles.repositories import ProfileRepository
from app.domain.users.models import UserAccount
from app.memory.metrics import MemoryMetrics
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.metrics import ModelGatewayMetricsSnapshot
from app.model_gateway.redaction import fingerprint_text


@dataclass(frozen=True, slots=True, kw_only=True)
class ParseProfileCommand:
    client_request_id: str
    user_message: str
    current_week: date
    context_scope_id: str | None = None
    run_id: UUID | None = None
    step_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ParseProfileResult:
    draft: ProfileAgentDraft
    created: bool


class ProfileAgentService:
    """Load minimum profile context, run the Agent, and store only a draft."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        drafts: ProfileAgentDraftRepository,
        agent: ProfileAgent,
        gateway: ModelGateway,
        contexts: ContextApplicationService,
        memory_metrics: MemoryMetrics,
        enabled: bool,
        supported_goals: tuple[str, ...],
        supported_constraint_types: tuple[str, ...],
        supported_equipment: tuple[str, ...],
        supported_locations: tuple[str, ...],
        draft_ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self._profiles = profiles
        self._drafts = drafts
        self._agent = agent
        self._gateway = gateway
        self._contexts = contexts
        self._memory_metrics = memory_metrics
        self._enabled = enabled
        self._supported_goals = supported_goals
        self._supported_constraint_types = supported_constraint_types
        self._supported_equipment = supported_equipment
        self._supported_locations = supported_locations
        self._draft_ttl = draft_ttl

    async def parse(
        self,
        user: UserAccount,
        command: ParseProfileCommand,
    ) -> ParseProfileResult:
        if not self._enabled:
            raise ModelGatewayUnavailable("The Model Gateway is disabled.")
        payload_fingerprint = self._payload_fingerprint(command)
        existing = await self._drafts.get_by_client_request_id(
            user.id,
            command.client_request_id,
        )
        if existing is not None:
            if existing.request_payload_fingerprint != payload_fingerprint:
                raise ProfileAgentIdempotencyConflict(
                    "The client request ID was already used with another payload."
                )
            return ParseProfileResult(existing, False)

        request_id = uuid4()
        draft_id = uuid4()
        context_snapshot = await self._contexts.build_snapshot(
            user,
            ContextBuildCommand(
                agent_type=AgentType.PROFILE_AGENT,
                current_task=self._current_task(command),
                profile_draft_id=draft_id,
                run_id=command.run_id,
                step_id=command.step_id,
            ),
            scope_id=(
                command.context_scope_id
                or f"profile-direct:{user.id}:{command.client_request_id.strip()}"
            ),
        )
        profile_snapshot = self._profile_snapshot(context_snapshot.context)
        self._memory_metrics.increment("profile_agent_context_builds")
        included_count = len(context_snapshot.reference.memory_versions)
        if included_count:
            self._memory_metrics.increment(
                "memories_injected_into_profile_agent", included_count
            )
        if context_snapshot.reference.degraded_mode is ContextDegradedMode.NO_MEMORY:
            self._memory_metrics.increment("profile_agent_no_memory_degraded")
        try:
            result = await self._agent.run(
                ProfileAgentInput(
                    user_message=command.user_message,
                    current_week=command.current_week,
                    existing_profile=profile_snapshot,
                    supported_goals=self._supported_goals,
                    supported_constraint_types=self._supported_constraint_types,
                    supported_equipment=self._supported_equipment,
                    supported_locations=self._supported_locations,
                    context=context_snapshot.context,
                    context_snapshot_reference_id=context_snapshot.reference.id,
                    context_fingerprint=context_snapshot.reference.context_fingerprint,
                    context_contract_version=context_snapshot.reference.contract_version,
                    context_degraded_mode=context_snapshot.reference.degraded_mode,
                    context_included_memory_count=included_count,
                ),
                user_id=user.id,
                request_id=request_id,
            )
        except ProfileAgentOutOfScopeError as exc:
            raise ProfileRequestOutOfScope(
                "The request is outside FitWeek's non-medical product scope.",
                request_id=exc.request_id,
            ) from exc
        created_at = utc_now()
        draft = ProfileAgentDraft(
            id=draft_id,
            request_id=result.request_id,
            client_request_id=command.client_request_id,
            user_id=user.id,
            request_payload_fingerprint=payload_fingerprint,
            input_fingerprint=result.input_fingerprint,
            output=result.output,
            prompt_version=result.prompt_version,
            provider_summary=result.provider_summary,
            fallback_used=result.fallback_used,
            fallback_type=result.fallback_type,
            created_at=created_at,
            expires_at=created_at + self._draft_ttl,
            status=ProfileDraftStatus.PENDING_REVIEW,
            version=1,
            context_snapshot_reference_id=context_snapshot.reference.id,
            context_fingerprint=context_snapshot.reference.context_fingerprint,
            context_contract_version=context_snapshot.reference.contract_version,
            context_policy_version=context_snapshot.reference.policy_version,
            context_degraded_mode=context_snapshot.reference.degraded_mode,
            context_included_memory_count=included_count,
        )
        try:
            saved = await self._drafts.save(draft)
        except RepositoryUniqueError as exc:
            raced = await self._drafts.get_by_client_request_id(
                user.id,
                command.client_request_id,
            )
            if (
                raced is None
                or raced.request_payload_fingerprint != payload_fingerprint
            ):
                raise ProfileAgentIdempotencyConflict(
                    "The client request ID was concurrently used with another payload."
                ) from exc
            return ParseProfileResult(raced, False)
        return ParseProfileResult(saved, True)

    async def get_draft(
        self,
        user: UserAccount,
        draft_id: UUID,
    ) -> ProfileAgentDraft:
        draft = await self._drafts.get(draft_id, user.id)
        if draft is None:
            raise ResourceNotFound("Profile Agent draft was not found or has expired.")
        return draft

    async def list_drafts(self, user: UserAccount) -> tuple[ProfileAgentDraft, ...]:
        return tuple(await self._drafts.list(user.id))

    async def list_traces(
        self,
        user: UserAccount,
        request_id: UUID,
    ) -> tuple[ModelCallTrace, ...]:
        return await self._gateway.list_traces(
            user_id=user.id,
            request_id=request_id,
        )

    def metrics(self) -> ModelGatewayMetricsSnapshot:
        return self._gateway.metrics()

    @staticmethod
    def _payload_fingerprint(command: ParseProfileCommand) -> str:
        payload = json.dumps(
            {
                "user_message": command.user_message,
                "current_week": command.current_week.isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return fingerprint_text(payload)

    @staticmethod
    def _current_task(command: ParseProfileCommand) -> dict[str, str]:
        message = command.user_message.casefold()
        task = {
            "request_type": "profile_parse",
            "current_week": command.current_week.isoformat(),
            "user_message_fingerprint": fingerprint_text(command.user_message),
        }
        controlled_signals = (
            ("preferred_time_of_day", "evening", ("evening", "晚上", "夜间")),
            ("preferred_time_of_day", "morning", ("morning", "早晨", "早上")),
            ("preferred_location", "HOME", ("at home", "home", "居家", "家里")),
            ("preferred_location", "GYM", ("gym", "健身房")),
        )
        seen: set[str] = set()
        for key, value, markers in controlled_signals:
            if key not in seen and any(marker in message for marker in markers):
                task[key] = value
                seen.add(key)
        return task

    @staticmethod
    def _profile_snapshot(context: BuiltContext) -> UserProfileSnapshot | None:
        """Read formal Profile facts only from the frozen Context payload."""

        values: dict[str, str] = {}
        for section in context.sections:
            if section.name is ContextSectionName.PROFILE_SNAPSHOT:
                values = {item.key: item.value for item in section.items}
                break
        required = {
            "experience_level",
            "weekly_frequency",
            "max_session_minutes",
            "primary_goal",
            "scope_confirmed",
        }
        if not required.issubset(values):
            return None
        return UserProfileSnapshot(
            experience_level=values["experience_level"],
            weekly_frequency=int(values["weekly_frequency"]),
            max_session_minutes=int(values["max_session_minutes"]),
            primary_goal=values["primary_goal"],
            scope_confirmed=values["scope_confirmed"].casefold() == "true",
        )
