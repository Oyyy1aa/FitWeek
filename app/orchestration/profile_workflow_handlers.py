"""Handlers for the explicit-user Profile Draft review workflow."""

from datetime import date
from typing import cast
from uuid import UUID

from app.application.errors import ApplicationError
from app.application.profile_agent import ParseProfileCommand, ProfileAgentService
from app.domain.orchestration.enums import StepOutcome, StepType
from app.domain.orchestration.models import JsonObject, JsonValue
from app.domain.profile_agent.apply_models import APPLY_POLICY_VERSION
from app.domain.profile_agent.models import ProfileDraftStatus
from app.domain.profile_agent.repositories import ProfileDraftReviewRepository
from app.domain.profiles.repositories import ProfileRepository
from app.domain.users.models import UserAccount
from app.orchestration.handler import (
    PermanentStepError,
    StepExecutionContext,
    StepExecutionResult,
)
from app.profile_application.apply import ProfileDraftApplyService
from app.profile_application.decision_codec import decision_from_payload


class ParseProfileRequestHandler:
    step_type = StepType.PARSE_PROFILE_REQUEST
    version = "phase-3b-parse-profile-v1"

    def __init__(self, *, service: ProfileAgentService, user: UserAccount) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        run = context.claim.run
        if run.user_id != self._user.id:
            raise PermanentStepError("Profile run belongs to another user.")
        try:
            result = await self._service.parse(
                self._user,
                ParseProfileCommand(
                    client_request_id=f"profile-run:{run.id}",
                    user_message=cast(str, run.input_payload["user_message"]),
                    current_week=date.fromisoformat(
                        cast(str, run.input_payload["current_week"])
                    ),
                    context_scope_id=f"profile-run-step:{context.claim.step.id}",
                    run_id=run.id,
                    step_id=context.claim.step.id,
                ),
            )
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError("Profile request parsing was rejected.") from exc
        draft = result.draft
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload={
                "draft_id": str(draft.id),
                "draft_version": draft.version,
                "request_id": str(draft.request_id),
                "scope_status": draft.output.scope_status.value,
                "snapshot_reference_id": str(draft.context_snapshot_reference_id),
                "context_fingerprint": draft.context_fingerprint,
                "contract_version": "profile-agent-context-v1",
                "included_memory_count": draft.context_included_memory_count,
                "degraded_mode": draft.context_degraded_mode.value,
            },
            result_reference=str(draft.id),
        )


class WaitForProfileDraftReviewHandler:
    step_type = StepType.WAIT_FOR_PROFILE_DRAFT_REVIEW
    version = "phase-3b-wait-profile-review-v1"

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        return StepExecutionResult(
            outcome=StepOutcome.WAITING_USER,
            output_payload=dict(context.claim.step.input_payload),
            result_reference=context.claim.run.result_reference,
        )


class ApplyProfileDraftHandler:
    step_type = StepType.APPLY_PROFILE_DRAFT
    version = "phase-3b-apply-profile-v1"

    def __init__(self, *, service: ProfileDraftApplyService, user: UserAccount) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        if context.claim.run.user_id != self._user.id:
            raise PermanentStepError("Profile run belongs to another user.")
        try:
            draft_id = UUID(cast(str, context.claim.step.input_payload["draft_id"]))
            decision_payload = cast(
                dict[str, JsonValue],
                context.claim.step.input_payload["decision"],
            )
            decision = decision_from_payload(decision_payload)
            committed = await self._service.apply(
                user=self._user,
                draft_id=draft_id,
                decision=decision,
            )
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError("Profile Draft apply was rejected.") from exc
        result = committed.result
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload={
                "draft_id": str(result.draft_id),
                "apply_result_id": str(result.id),
                "profile_id": str(result.profile_id),
                "resulting_profile_version": result.resulting_profile_version,
                "added_constraint_ids": [
                    str(item) for item in result.added_constraint_ids
                ],
                "handler_version": self.version,
                "apply_policy_version": APPLY_POLICY_VERSION,
            },
            result_reference=str(result.id),
        )


class FinalizeProfileRunHandler:
    step_type = StepType.FINALIZE_PROFILE_RUN
    version = "phase-3b-finalize-profile-v1"

    def __init__(
        self,
        *,
        reviews: ProfileDraftReviewRepository,
        profiles: ProfileRepository,
        apply_service: ProfileDraftApplyService,
        user: UserAccount,
    ) -> None:
        self._reviews = reviews
        self._profiles = profiles
        self._apply_service = apply_service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        try:
            payload: JsonObject = dict(context.claim.step.input_payload)
            draft_id = UUID(cast(str, payload["draft_id"]))
            expected_result_id = UUID(cast(str, payload["apply_result_id"]))
            expected_profile_version = cast(int, payload["resulting_profile_version"])
            draft = await self._reviews.get_draft_for_review(
                draft_id,
                self._user.id,
            )
            result = await self._apply_service.get_apply_result(
                user=self._user,
                draft_id=draft_id,
            )
            profile = await self._profiles.get_by_user_id(self._user.id)
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError(
                "Profile run finalization input is invalid."
            ) from exc
        if (
            draft is None
            or draft.status is not ProfileDraftStatus.APPLIED
            or result.id != expected_result_id
            or profile is None
            or profile.id != result.profile_id
            or profile.version != expected_profile_version
            or profile.version != result.resulting_profile_version
        ):
            raise PermanentStepError("Applied Profile state is not finalizable.")
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=payload,
            result_reference=str(result.id),
        )
