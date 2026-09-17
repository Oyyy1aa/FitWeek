"""Safe-reference handlers for the Phase 7B Recovery application workflow."""

from typing import cast
from uuid import UUID

from app.application.errors import ApplicationError
from app.application.recovery_applications import RecoveryApplicationService
from app.domain.orchestration.enums import StepOutcome, StepType
from app.domain.orchestration.models import JsonObject
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.recovery.enums import RecoveryDraftStatus
from app.domain.recovery_application.models import ApplyRecoveryDraftCommand
from app.domain.scheduling.enums import ScheduleDraftOutcome, ScheduleDraftStatus
from app.domain.scheduling.repositories import ScheduleDraftRepository
from app.domain.session_design.enums import SessionDesignDraftStatus
from app.domain.session_design.repositories import SessionDesignRepository
from app.domain.users.models import UserAccount
from app.orchestration.handler import (
    PermanentStepError,
    StepExecutionContext,
    StepExecutionResult,
)


def _command(payload: JsonObject) -> ApplyRecoveryDraftCommand:
    action_ids = cast(list[object], payload["selected_action_candidate_ids"])
    return ApplyRecoveryDraftCommand(
        client_request_id=cast(str, payload["apply_client_request_id"]),
        expected_draft_version=cast(int, payload["expected_draft_version"]),
        root_plan_id=UUID(cast(str, payload["root_plan_id"])),
        source_revision=cast(int, payload["source_revision"]),
        expected_plan_version=cast(int, payload["expected_plan_version"]),
        selected_action_candidate_ids=tuple(UUID(str(item)) for item in action_ids),
    )


class _PreviewRecoveryHandler:
    step_type: StepType
    version = "phase-7b-preview-recovery-application-v1"

    def __init__(self, service: RecoveryApplicationService, user: UserAccount) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(
            context.claim.run.input_payload
            if self.step_type is StepType.LOAD_RECOVERY_APPLICATION_CONTEXT
            else context.claim.step.input_payload
        )
        try:
            draft_id = UUID(cast(str, payload["recovery_draft_id"]))
            preview = await self._service.preview(
                self._user, draft_id, _command(payload)
            )
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError("Recovery application validation failed.") from exc
        output: JsonObject = {
            **payload,
            "application_fingerprint": preview.application_fingerprint,
            "requires_session_design_drafts": (preview.requires_session_design_drafts),
            "requires_schedule_draft": preview.requires_schedule_draft,
            "requires_new_plan_revision": preview.requires_new_plan_revision,
            "requires_calendar_reconciliation": (
                preview.requires_calendar_reconciliation
            ),
            "frequency_before": preview.frequency_before,
            "frequency_after": preview.frequency_after,
            "immutable_session_ids": [
                str(item) for item in preview.immutable_session_ids
            ],
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(draft_id),
        )


class LoadRecoveryApplicationContextHandler(_PreviewRecoveryHandler):
    step_type = StepType.LOAD_RECOVERY_APPLICATION_CONTEXT
    version = "phase-7b-load-recovery-context-v1"


class ValidateRecoveryDraftHandler(_PreviewRecoveryHandler):
    step_type = StepType.VALIDATE_RECOVERY_DRAFT
    version = "phase-7b-validate-recovery-draft-v1"


class ResolveRecoveryActionsHandler(_PreviewRecoveryHandler):
    step_type = StepType.RESOLVE_RECOVERY_ACTIONS
    version = "phase-7b-resolve-recovery-actions-v1"


class CreateRecoverySubdraftsHandler:
    step_type = StepType.CREATE_RECOVERY_SUBDRAFTS
    version = "phase-7b-create-recovery-subdrafts-v1"

    def __init__(self, service: RecoveryApplicationService, user: UserAccount) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        try:
            draft_id = UUID(cast(str, payload["recovery_draft_id"]))
            children = await self._service.create_subdrafts(
                self._user, draft_id, _command(payload)
            )
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError("Recovery child Draft creation failed.") from exc
        output: JsonObject = {
            **payload,
            "session_design_draft_ids": [
                str(item) for item in children.session_design_draft_ids
            ],
            "schedule_draft_ids": [str(item) for item in children.schedule_draft_ids],
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(draft_id),
        )


class WaitForRecoverySubdraftReviewsHandler:
    step_type = StepType.WAIT_FOR_RECOVERY_SUBDRAFT_REVIEWS
    version = "phase-7b-wait-recovery-subdraft-reviews-v1"

    def __init__(
        self,
        *,
        session_designs: SessionDesignRepository,
        schedules: ScheduleDraftRepository,
        user: UserAccount,
    ) -> None:
        self._session_designs = session_designs
        self._schedules = schedules
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        design_ids = tuple(
            UUID(str(item))
            for item in cast(list[object], payload.get("session_design_draft_ids", []))
        )
        schedule_ids = tuple(
            UUID(str(item))
            for item in cast(list[object], payload.get("schedule_draft_ids", []))
        )
        pending = False
        for draft_id in design_ids:
            design = await self._session_designs.get_draft(self._user.id, draft_id)
            if design is None:
                raise PermanentStepError("A Session Design child Draft is missing.")
            if design.status in {
                SessionDesignDraftStatus.REJECTED,
                SessionDesignDraftStatus.EXPIRED,
            }:
                raise PermanentStepError("A Session Design child Draft was rejected.")
            pending = (
                pending or design.status is SessionDesignDraftStatus.PENDING_REVIEW
            )
        for draft_id in schedule_ids:
            schedule = await self._schedules.get_draft(self._user.id, draft_id)
            if schedule is None:
                raise PermanentStepError("A Schedule child Draft is missing.")
            if schedule.outcome is ScheduleDraftOutcome.PARTIAL:
                raise PermanentStepError("A partial Schedule child Draft is unusable.")
            if schedule.status in {
                ScheduleDraftStatus.REJECTED,
                ScheduleDraftStatus.EXPIRED,
            }:
                raise PermanentStepError("A Schedule child Draft was rejected.")
            pending = pending or schedule.status is ScheduleDraftStatus.PENDING_REVIEW
        return StepExecutionResult(
            outcome=(StepOutcome.WAITING_USER if pending else StepOutcome.SUCCEEDED),
            output_payload=payload,
            result_reference=context.claim.run.result_reference,
        )


class BuildRecoveryPlanRevisionHandler:
    step_type = StepType.BUILD_RECOVERY_PLAN_REVISION
    version = "phase-7b-build-recovery-plan-revision-v1"

    def __init__(self, service: RecoveryApplicationService, user: UserAccount) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        try:
            draft_id = UUID(cast(str, payload["recovery_draft_id"]))
            outcome = await self._service.apply(self._user, draft_id, _command(payload))
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError("Recovery Plan Revision creation failed.") from exc
        output: JsonObject = {
            **payload,
            "application_result_id": str(outcome.result.id),
            "application_outcome": outcome.result.outcome.value,
            "created_revision": outcome.result.created_revision,
            "resulting_plan_version": outcome.plan.version if outcome.plan else None,
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(outcome.result.id),
        )


class VerifyRecoveryPlanSafetyHandler:
    step_type = StepType.VERIFY_RECOVERY_PLAN_SAFETY
    version = "phase-7b-verify-recovery-plan-safety-v1"

    def __init__(self, service: RecoveryApplicationService, user: UserAccount) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        try:
            await self._service.verify_result(
                self._user, UUID(cast(str, payload["recovery_draft_id"]))
            )
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError(
                "Recovery Plan Safety verification failed."
            ) from exc
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload={**payload, "safety_passed": True},
            result_reference=cast(str | None, payload.get("application_result_id")),
        )


class WaitForRecoveryRevisionConfirmationHandler:
    step_type = StepType.WAIT_FOR_RECOVERY_REVISION_CONFIRMATION
    version = "phase-7b-wait-recovery-confirmation-v1"

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        return StepExecutionResult(
            outcome=(
                StepOutcome.WAITING_USER
                if payload.get("created_revision") is not None
                else StepOutcome.SUCCEEDED
            ),
            output_payload=payload,
            result_reference=cast(str | None, payload.get("application_result_id")),
        )


class FinalizeRecoveryApplicationHandler:
    step_type = StepType.FINALIZE_RECOVERY_APPLICATION
    version = "phase-7b-finalize-recovery-application-v1"

    def __init__(
        self,
        *,
        service: RecoveryApplicationService,
        plans: PlanRepository,
        user: UserAccount,
    ) -> None:
        self._service = service
        self._plans = plans
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        try:
            outcome = await self._service.get_result(
                self._user, UUID(cast(str, payload["recovery_draft_id"]))
            )
            current = (
                await self._plans.get_current_confirmed(
                    outcome.result.root_plan_id, self._user.id
                )
                if outcome.plan is not None
                else None
            )
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError("Recovery application cannot finalize.") from exc
        if outcome.draft.status is not RecoveryDraftStatus.APPLIED:
            raise PermanentStepError("Recovery Draft is not applied.")
        if outcome.plan is not None and (
            outcome.plan.status is not WeeklyPlanStatus.CONFIRMED
            or current is None
            or current.id != outcome.plan.id
        ):
            raise PermanentStepError("Recovery Revision is not current and confirmed.")
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=payload,
            result_reference=str(outcome.result.id),
        )
