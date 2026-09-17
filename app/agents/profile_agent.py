"""Pure Profile Agent contract pipeline over the provider-neutral gateway."""

import json
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.domain.context.enums import ContextSectionName
from app.domain.model_gateway.errors import ModelSchemaInvalidError
from app.domain.model_gateway.models import ModelRequest, ModelTraceContext
from app.domain.profile_agent.models import (
    ProfileAgentInput,
    ProfileAgentOutput,
    ProfileAgentResult,
    ScopeStatus,
)
from app.domain.profile_agent.scope import DeterministicScopeGuard
from app.domain.profile_agent.validation import ProfileAgentBusinessValidator
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.json_extractor import extract_json_object
from app.model_gateway.redaction import fingerprint_text
from app.prompts.registry import PromptRegistry


class ProfileAgentOutOfScopeError(RuntimeError):
    """Raised only after a deterministic pre-provider scope block."""

    def __init__(self, request_id: UUID) -> None:
        super().__init__("The request is outside FitWeek's supported product scope.")
        self.request_id = request_id


class ProfileAgent:
    """Build a reviewable draft; it has no repository or business-write access."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        prompts: PromptRegistry,
        scope_guard: DeterministicScopeGuard | None = None,
        validator: ProfileAgentBusinessValidator | None = None,
        prompt_version: str = "profile-agent-v1",
        request_timeout_seconds: float = 10,
        max_response_bytes: int = 262144,
    ) -> None:
        self._gateway = gateway
        self._prompts = prompts
        self._scope_guard = scope_guard or DeterministicScopeGuard()
        self._validator = validator or ProfileAgentBusinessValidator()
        self._prompt_version = prompt_version
        self._request_timeout_seconds = request_timeout_seconds
        self._max_response_bytes = max_response_bytes

    async def run(
        self,
        input_value: ProfileAgentInput,
        *,
        user_id: UUID,
        request_id: UUID | None = None,
    ) -> ProfileAgentResult:
        active_request_id = request_id or uuid4()
        prompt = self._prompts.get("profile-agent", self._prompt_version)
        fingerprint = self._input_fingerprint(input_value)
        trace_context = ModelTraceContext(
            user_id=user_id,
            agent_name="profile-agent",
            prompt_name=prompt.name,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
            input_fingerprint=fingerprint,
            context_snapshot_reference_id=input_value.context_snapshot_reference_id,
            context_fingerprint=input_value.context_fingerprint,
            context_contract_version=input_value.context_contract_version,
            context_degraded_mode=input_value.context_degraded_mode,
        )
        scope = self._scope_guard.classify(input_value.user_message)
        if scope.status is ScopeStatus.OUT_OF_SCOPE:
            await self._gateway.record_scope_block(
                request_id=active_request_id,
                trace_context=trace_context,
            )
            raise ProfileAgentOutOfScopeError(active_request_id)
        if scope.status is ScopeStatus.NEEDS_REVIEW:
            await self._gateway.record_scope_block(
                request_id=active_request_id,
                trace_context=trace_context,
                needs_review=True,
            )
            return ProfileAgentResult(
                request_id=active_request_id,
                output=self._needs_review_template(
                    "The request contains an ambiguous health-related statement "
                    "and needs human review."
                ),
                input_fingerprint=fingerprint,
                prompt_version=prompt.version,
                provider_summary="deterministic-scope-guard",
                fallback_used=False,
                fallback_type=None,
            )

        context = self._structured_context(input_value)
        user_prompt = prompt.render_user(
            context_json=json.dumps(
                context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            user_message_json=json.dumps(input_value.user_message, ensure_ascii=False),
        )
        request = ModelRequest(
            request_id=active_request_id,
            model="gateway-selected",
            system_prompt=prompt.system_template,
            user_prompt=user_prompt,
            response_schema_name=prompt.response_schema_name,
            temperature=0,
            max_output_tokens=1200,
            timeout_seconds=self._request_timeout_seconds,
            metadata={"agent": "profile-agent", "prompt_version": prompt.version},
        )

        def validate(raw_text: str) -> ProfileAgentOutput:
            document = extract_json_object(
                raw_text,
                max_bytes=self._max_response_bytes,
            )
            try:
                output = ProfileAgentOutput.model_validate_json(
                    json.dumps(document, ensure_ascii=False),
                    strict=True,
                )
            except ValidationError as exc:
                raise ModelSchemaInvalidError() from exc
            return self._validator.validate(output, input_value)

        result = await self._gateway.invoke(
            request=request,
            trace_context=trace_context,
            validator=validate,
            template_factory=self._needs_review_template,
        )
        return ProfileAgentResult(
            request_id=active_request_id,
            output=result.value,
            input_fingerprint=fingerprint,
            prompt_version=prompt.version,
            provider_summary=(
                f"{result.provider_name}:{result.provider_version}:{result.model}"
            ),
            fallback_used=result.fallback_used,
            fallback_type=result.fallback_type,
        )

    @staticmethod
    def _minimal_context(input_value: ProfileAgentInput) -> dict[str, object]:
        profile: dict[str, object] | None = None
        if input_value.existing_profile is not None:
            profile = {
                "experience_level": input_value.existing_profile.experience_level,
                "weekly_frequency": input_value.existing_profile.weekly_frequency,
                "max_session_minutes": (
                    input_value.existing_profile.max_session_minutes
                ),
                "primary_goal": input_value.existing_profile.primary_goal,
                "scope_confirmed": input_value.existing_profile.scope_confirmed,
            }
        return {
            "current_week": input_value.current_week.isoformat(),
            "existing_profile": profile,
            "supported_goals": input_value.supported_goals,
            "supported_constraint_types": input_value.supported_constraint_types,
            "supported_equipment": input_value.supported_equipment,
            "supported_locations": input_value.supported_locations,
        }

    @classmethod
    def _structured_context(cls, input_value: ProfileAgentInput) -> dict[str, object]:
        sections: dict[str, list[dict[str, str | None]]] = {}
        if input_value.context is not None:
            for section in input_value.context.sections:
                if section.name is ContextSectionName.SYSTEM_POLICY:
                    continue
                sections[section.name.value] = [
                    {
                        "key": item.key,
                        "value": item.value,
                        "source": item.source,
                        "source_reference": item.source_reference,
                    }
                    for item in section.items
                ]
        return {
            **cls._minimal_context(input_value),
            "context_data": sections,
            "context_metadata": {
                "snapshot_reference_id": (
                    str(input_value.context_snapshot_reference_id)
                    if input_value.context_snapshot_reference_id
                    else None
                ),
                "context_fingerprint": input_value.context_fingerprint,
                "contract_version": input_value.context_contract_version,
                "degraded_mode": input_value.context_degraded_mode.value,
            },
        }

    @classmethod
    def _input_fingerprint(cls, input_value: ProfileAgentInput) -> str:
        payload = {
            "user_message": input_value.user_message,
            "context_fingerprint": input_value.context_fingerprint,
            **cls._minimal_context(input_value),
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return fingerprint_text(serialized)

    @staticmethod
    def _needs_review_template(
        explanation: str = (
            "Model providers were unavailable; human review is required."
        ),
    ) -> ProfileAgentOutput:
        return ProfileAgentOutput(
            weekly_frequency=None,
            max_session_minutes=None,
            goals=(),
            equipment=(),
            locations=(),
            hard_constraints=(),
            soft_preferences=(),
            temporary_constraints=(),
            scope_status=ScopeStatus.NEEDS_REVIEW,
            missing_fields=(
                "weekly_frequency",
                "max_session_minutes",
                "primary_goal",
                "available_equipment",
                "available_location",
            ),
            memory_candidates=(),
            explanation_summary=explanation,
        )
