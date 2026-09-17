"""Pure controlled Session Designer over Gateway and frozen inputs."""

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.domain.context.models import FrozenContextSnapshot
from app.domain.exercises.models import Exercise
from app.domain.model_gateway.errors import (
    ModelBusinessValidationError,
    ModelSchemaInvalidError,
)
from app.domain.model_gateway.models import (
    ModelCallTrace,
    ModelRequest,
    ModelTraceContext,
)
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.domain.session_design.models import ExerciseCandidateSet, SessionDesignerOutput
from app.domain.session_design.templates import SessionTemplate
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.json_extractor import extract_json_object
from app.prompts.registry import PromptRegistry
from app.session_design.fallback import DeterministicSessionFallback
from app.session_design.validator import SessionDesignerBusinessValidator


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDesignerAgentResult:
    request_id: UUID
    output: SessionDesignerOutput
    prompt_version: str
    provider_summary: str
    fallback_used: bool
    provider_attempts: int


class SessionDesignerAgent:
    """Select from frozen IDs; this class has no repository or write access."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        prompts: PromptRegistry,
        prompt_version: str = "session-designer-v1",
        request_timeout_seconds: float = 10,
        max_response_bytes: int = 262144,
    ) -> None:
        self._gateway = gateway
        self._prompts = prompts
        self._prompt_version = prompt_version
        self._timeout = request_timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._validator = SessionDesignerBusinessValidator()
        self._fallback = DeterministicSessionFallback()

    async def run(
        self,
        *,
        user_id: UUID,
        request_fingerprint: str,
        candidate_set: ExerciseCandidateSet,
        template: SessionTemplate,
        context: FrozenContextSnapshot,
        profile: FitnessProfile,
        constraints: tuple[UserConstraint, ...],
        catalog: dict[str, Exercise],
        request_id: UUID | None = None,
    ) -> SessionDesignerAgentResult:
        active_request_id = request_id or uuid4()
        prompt = self._prompts.get("session-designer", self._prompt_version)
        structured = {
            "context_snapshot_reference": str(context.reference.id),
            "context_fingerprint": context.reference.context_fingerprint,
            "candidate_set_id": str(candidate_set.id),
            "candidate_set_fingerprint": candidate_set.fingerprint,
            "template_id": template.id.value,
            "template_version": template.version,
            "session_type": template.session_type.value,
            "slots": [
                {
                    "slot_id": item.slot_id,
                    "role": item.role.value,
                    "allowed_exercise_ids": item.exercise_ids,
                }
                for item in candidate_set.slots
            ],
        }
        user_prompt = prompt.render_user(
            context_json=json.dumps(structured, sort_keys=True, separators=(",", ":")),
            user_message_json=json.dumps(
                "structured-session-design", ensure_ascii=False
            ),
        )
        request = ModelRequest(
            request_id=active_request_id,
            model="gateway-selected",
            system_prompt=prompt.system_template,
            user_prompt=user_prompt,
            response_schema_name=prompt.response_schema_name,
            temperature=0,
            max_output_tokens=1000,
            timeout_seconds=self._timeout,
            metadata={"agent": "session-designer", "prompt_version": prompt.version},
        )

        def validate(raw_text: str) -> SessionDesignerOutput:
            document = extract_json_object(raw_text, max_bytes=self._max_response_bytes)
            try:
                output = SessionDesignerOutput.model_validate_json(
                    json.dumps(document, ensure_ascii=False), strict=True
                )
            except ValidationError as exc:
                raise ModelSchemaInvalidError() from exc
            try:
                return self._validator.validate(
                    output,
                    candidate_set=candidate_set,
                    template=template,
                    profile=profile,
                    constraints=constraints,
                    catalog=catalog,
                )
            except ValueError as exc:
                raise ModelBusinessValidationError() from exc

        result = await self._gateway.invoke(
            request=request,
            trace_context=ModelTraceContext(
                user_id=user_id,
                agent_name="session-designer",
                prompt_name=prompt.name,
                prompt_version=prompt.version,
                prompt_sha256=prompt.sha256,
                input_fingerprint=request_fingerprint,
                context_snapshot_reference_id=context.reference.id,
                context_fingerprint=context.reference.context_fingerprint,
                context_contract_version=context.reference.contract_version,
                context_degraded_mode=context.reference.degraded_mode,
            ),
            validator=validate,
            template_factory=lambda: self._fallback.build(candidate_set, template),
        )
        return SessionDesignerAgentResult(
            request_id=active_request_id,
            output=result.value,
            prompt_version=prompt.version,
            provider_summary=f"{result.provider_name}:{result.provider_version}:{result.model}",
            fallback_used=result.fallback_used,
            provider_attempts=result.provider_attempts,
        )

    async def list_traces(
        self, *, user_id: UUID, request_id: UUID
    ) -> tuple[ModelCallTrace, ...]:
        """Expose only safe gateway traces to the application layer."""

        return await self._gateway.list_traces(
            user_id=user_id,
            request_id=request_id,
        )
