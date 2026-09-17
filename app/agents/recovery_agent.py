"""Model-facing Recovery boundary that can only select frozen Candidate IDs."""

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.behavior.evidence_policy import time_bucket
from app.domain.behavior.models import BehaviorSummary
from app.domain.context.models import FrozenContextSnapshot
from app.domain.model_gateway.errors import (
    ModelBusinessValidationError,
    ModelSchemaInvalidError,
)
from app.domain.model_gateway.models import (
    ModelCallTrace,
    ModelRequest,
    ModelTraceContext,
)
from app.domain.plans.models import WeeklyPlan
from app.domain.recovery.enums import RecoveryRequestType
from app.domain.recovery.models import (
    RecoveryActionCandidateSet,
    RecoveryAgentOutput,
    RecoveryChangeImpactSnapshot,
)
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.json_extractor import extract_json_object
from app.prompts.registry import PromptRegistry
from app.recovery.deterministic_fallback import DeterministicRecoveryFallback
from app.recovery.validator import RecoveryAgentBusinessValidator


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryAgentResult:
    request_id: UUID
    output: RecoveryAgentOutput
    prompt_version: str
    provider_name: str
    provider_version: str
    fallback_used: bool
    provider_attempts: int


class RecoveryAgent:
    """No Repository, Plan mutation, Memory, or Calendar dependency is accepted."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        prompts: PromptRegistry,
        request_timeout_seconds: float = 10,
        max_response_bytes: int = 262144,
    ) -> None:
        self._gateway = gateway
        self._prompts = prompts
        self._timeout = request_timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._validator = RecoveryAgentBusinessValidator()
        self._fallback = DeterministicRecoveryFallback()

    async def run(
        self,
        *,
        user_id: UUID,
        request_fingerprint: str,
        request_type: RecoveryRequestType,
        candidate_set: RecoveryActionCandidateSet,
        behavior_summary: BehaviorSummary,
        impact: RecoveryChangeImpactSnapshot,
        plan: WeeklyPlan,
        context: FrozenContextSnapshot,
        request_id: UUID | None = None,
    ) -> RecoveryAgentResult:
        active_id = request_id or uuid4()
        prompt = self._prompts.get("recovery-agent", "recovery-agent-v1")
        structured = {
            "context_snapshot_reference_id": str(context.reference.id),
            "context_fingerprint": context.reference.context_fingerprint,
            "behavior_summary_id": str(candidate_set.behavior_summary_id),
            "behavior_summary": {
                "completed_count": behavior_summary.completed_count,
                "partially_completed_count": (
                    behavior_summary.partially_completed_count
                ),
                "skipped_count": behavior_summary.skipped_count,
                "missing_checkin_count": behavior_summary.missing_checkin_count,
                "patterns": [
                    {
                        "pattern_id": item.pattern_id,
                        "type": item.pattern_type.value,
                        "key": item.key,
                    }
                    for item in sorted(
                        {
                            item.pattern_id: item
                            for group in (
                                behavior_summary.repeated_time_patterns,
                                behavior_summary.repeated_location_patterns,
                                behavior_summary.repeated_skip_patterns,
                            )
                            for item in group
                        }.values(),
                        key=lambda item: item.pattern_id,
                    )
                ],
            },
            "change_impact_snapshot_id": str(impact.id),
            "future_session_ids": [str(item) for item in impact.mutable_session_ids],
            "future_sessions": [
                {
                    "session_id": str(item.id),
                    "scheduled_day": item.scheduled_start.strftime("%A").upper(),
                    "time_of_day": time_bucket(item.scheduled_start),
                    "duration_minutes": item.estimated_minutes,
                    "location_type": item.location_type.value,
                    "session_type": item.session_type.value,
                    "mutable": True,
                }
                for item in plan.sessions
                if item.id in set(impact.mutable_session_ids)
            ],
            "immutable_session_ids": [
                str(item) for item in impact.immutable_session_ids
            ],
            "allowed_action_candidates": [
                {
                    "candidate_id": str(item.id),
                    "action_type": item.action_type.value,
                    "target_session_id": (
                        str(item.target_session_id) if item.target_session_id else None
                    ),
                    "redesign_goal": (
                        item.redesign_goal.value if item.redesign_goal else None
                    ),
                }
                for item in candidate_set.candidates
            ],
        }
        request = ModelRequest(
            request_id=active_id,
            model="gateway-selected",
            system_prompt=prompt.system_template,
            user_prompt=prompt.render_user(
                context_json=json.dumps(
                    structured, sort_keys=True, separators=(",", ":")
                ),
                user_message_json=json.dumps(request_type.value),
            ),
            response_schema_name=prompt.response_schema_name,
            temperature=0,
            max_output_tokens=800,
            timeout_seconds=self._timeout,
            metadata={"agent": "recovery-agent", "prompt_version": prompt.version},
        )

        def validate(raw_text: str) -> RecoveryAgentOutput:
            document = extract_json_object(raw_text, max_bytes=self._max_response_bytes)
            try:
                output = RecoveryAgentOutput.model_validate_json(
                    json.dumps(document, ensure_ascii=False), strict=True
                )
            except ValidationError as exc:
                raise ModelSchemaInvalidError() from exc
            try:
                return self._validator.validate(
                    output,
                    candidate_set=candidate_set,
                    impact=impact,
                    plan=plan,
                    check_spacing=False,
                )
            except ValueError as exc:
                raise ModelBusinessValidationError() from exc

        gateway_result = await self._gateway.invoke(
            request=request,
            trace_context=ModelTraceContext(
                user_id=user_id,
                agent_name="recovery-agent",
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
            template_factory=lambda: self._fallback.build(
                candidate_set,
                request_type=request_type,
                impact=impact,
            ),
        )
        return RecoveryAgentResult(
            request_id=active_id,
            output=gateway_result.value,
            prompt_version=prompt.version,
            provider_name=gateway_result.provider_name,
            provider_version=gateway_result.provider_version,
            fallback_used=gateway_result.fallback_used,
            provider_attempts=gateway_result.provider_attempts,
        )

    async def list_traces(
        self, *, user_id: UUID, request_id: UUID
    ) -> tuple[ModelCallTrace, ...]:
        return await self._gateway.list_traces(user_id=user_id, request_id=request_id)
