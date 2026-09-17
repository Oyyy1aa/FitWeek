"""Handlers for explicit Session Design Plan Revision integration."""

from typing import cast
from uuid import UUID

from app.application.errors import ApplicationError
from app.application.session_design_application import (
    SessionDesignPlanApplicationService,
)
from app.domain.context.repositories import ContextSnapshotRepository
from app.domain.orchestration.enums import StepOutcome, StepType
from app.domain.orchestration.models import JsonObject
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.session_design.enums import SessionDesignDraftStatus
from app.domain.session_design.repositories import SessionDesignRepository
from app.domain.session_design_application.models import (
    APPLICATION_POLICY_VERSION,
    ApplySessionDesignCommand,
)
from app.domain.users.models import UserAccount
from app.orchestration.handler import (
    PermanentStepError,
    StepExecutionContext,
    StepExecutionResult,
)


def _command(payload: JsonObject) -> ApplySessionDesignCommand:
    return ApplySessionDesignCommand(
        client_request_id=cast(str, payload["apply_client_request_id"]),
        expected_draft_version=cast(int, payload["expected_draft_version"]),
        root_plan_id=UUID(cast(str, payload["root_plan_id"])),
        source_revision=cast(int, payload["source_revision"]),
        expected_plan_version=cast(int, payload["expected_plan_version"]),
        target_session_id=UUID(cast(str, payload["target_session_id"])),
    )


class LoadSessionApplicationContextHandler:
    step_type = StepType.LOAD_SESSION_APPLICATION_CONTEXT
    version = "phase-5b-load-session-application-v1"

    def __init__(
        self,
        *,
        drafts: SessionDesignRepository,
        contexts: ContextSnapshotRepository,
        plans: PlanRepository,
        user: UserAccount,
    ) -> None:
        self._drafts = drafts
        self._contexts = contexts
        self._plans = plans
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        try:
            payload = dict(context.claim.run.input_payload)
            draft_id = UUID(cast(str, payload["draft_id"]))
            root_plan_id = UUID(cast(str, payload["root_plan_id"]))
            source_revision = cast(int, payload["source_revision"])
            draft = await self._drafts.get_draft(self._user.id, draft_id)
            source = await self._plans.get_revision_for_user(
                root_plan_id, self._user.id, source_revision
            )
            if draft is None or source is None:
                raise ValueError("Application references were not found.")
            candidates = await self._drafts.get_candidate_set(
                self._user.id, draft.candidate_set_id
            )
            snapshot = await self._contexts.get(
                self._user.id, draft.context_snapshot_reference_id
            )
            if candidates is None or snapshot is None:
                raise ValueError("Frozen Session Design references were not found.")
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError(
                "Session application context could not be loaded."
            ) from exc
        output: JsonObject = {
            **payload,
            "draft_version": draft.version,
            "candidate_set_id": str(candidates.id),
            "context_snapshot_reference_id": str(snapshot.reference.id),
            "source_plan_id": str(source.id),
            "policy_version": APPLICATION_POLICY_VERSION,
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(draft.id),
        )


class ValidateSessionDesignTargetHandler:
    step_type = StepType.VALIDATE_SESSION_DESIGN_TARGET
    version = "phase-5b-validate-session-target-v1"

    def __init__(
        self,
        *,
        service: SessionDesignPlanApplicationService,
        user: UserAccount,
    ) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        try:
            draft_id = UUID(cast(str, payload["draft_id"]))
            preview = await self._service.preview(
                self._user, draft_id, _command(payload)
            )
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError(
                "Session Design target validation was rejected."
            ) from exc
        output: JsonObject = {
            **payload,
            "application_fingerprint": preview.application_fingerprint,
            "safety_passed": preview.validation.passed,
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(draft_id),
        )


class BuildSessionPlanRevisionHandler:
    step_type = StepType.BUILD_SESSION_PLAN_REVISION
    version = "phase-5b-build-session-revision-v1"

    def __init__(
        self,
        *,
        service: SessionDesignPlanApplicationService,
        user: UserAccount,
    ) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        try:
            draft_id = UUID(cast(str, payload["draft_id"]))
            outcome = await self._service.apply(self._user, draft_id, _command(payload))
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError(
                "Session Design Plan Revision could not be created."
            ) from exc
        output: JsonObject = {
            **payload,
            "application_result_id": str(outcome.result.id),
            "created_revision": outcome.result.created_revision,
            "created_plan_revision_id": str(outcome.plan.id),
            "resulting_plan_version": outcome.plan.version,
            "draft_version": outcome.draft.version,
            "application_created": outcome.created,
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(outcome.result.id),
        )


class VerifySessionPlanSafetyHandler:
    step_type = StepType.VERIFY_SESSION_PLAN_SAFETY
    version = "phase-5b-verify-session-plan-safety-v1"

    def __init__(
        self,
        *,
        service: SessionDesignPlanApplicationService,
        user: UserAccount,
    ) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        try:
            result_id = UUID(cast(str, payload["application_result_id"]))
            outcome = await self._service.verify_result(self._user, result_id)
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError(
                "The created Session Design Plan Revision failed verification."
            ) from exc
        output: JsonObject = {
            **payload,
            "created_revision": outcome.plan.revision,
            "resulting_plan_version": outcome.plan.version,
            "safety_passed": True,
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(outcome.result.id),
        )


class WaitForPlanRevisionConfirmationHandler:
    step_type = StepType.WAIT_FOR_PLAN_REVISION_CONFIRMATION
    version = "phase-5b-wait-plan-confirmation-v1"

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        return StepExecutionResult(
            outcome=StepOutcome.WAITING_USER,
            output_payload=dict(context.claim.step.input_payload),
            result_reference=context.claim.run.result_reference,
        )


class FinalizeSessionApplicationHandler:
    step_type = StepType.FINALIZE_SESSION_APPLICATION
    version = "phase-5b-finalize-session-application-v1"

    def __init__(
        self,
        *,
        service: SessionDesignPlanApplicationService,
        plans: PlanRepository,
        user: UserAccount,
    ) -> None:
        self._service = service
        self._plans = plans
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        try:
            result_id = UUID(cast(str, payload["application_result_id"]))
            root_plan_id = UUID(cast(str, payload["root_plan_id"]))
            created_revision = cast(int, payload["created_revision"])
            outcome = await self._service.verify_result(self._user, result_id)
            current = await self._plans.get_current_confirmed(
                root_plan_id, self._user.id
            )
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError(
                "Session Design application cannot be finalized."
            ) from exc
        if (
            outcome.plan.revision != created_revision
            or outcome.plan.status is not WeeklyPlanStatus.CONFIRMED
            or current is None
            or current.id != outcome.plan.id
            or outcome.draft.status is not SessionDesignDraftStatus.APPLIED
        ):
            raise PermanentStepError(
                "Confirmed Session Design Plan state is not finalizable."
            )
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=payload,
            result_reference=str(outcome.result.id),
        )
