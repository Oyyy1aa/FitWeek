"""Handlers for the Schedule Draft to Plan Revision workflow."""

from typing import cast
from uuid import UUID

from app.application.errors import ApplicationError
from app.application.schedule_application import SchedulePlanApplicationService
from app.domain.orchestration.enums import StepOutcome, StepType
from app.domain.orchestration.models import JsonObject
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.schedule_application.models import ApplyScheduleDraftCommand
from app.domain.scheduling.enums import ScheduleDraftStatus
from app.domain.scheduling.repositories import ScheduleDraftRepository
from app.domain.users.models import UserAccount
from app.orchestration.handler import (
    PermanentStepError,
    StepExecutionContext,
    StepExecutionResult,
)


def _command(payload: JsonObject) -> ApplyScheduleDraftCommand:
    return ApplyScheduleDraftCommand(
        client_request_id=cast(str, payload["apply_client_request_id"]),
        expected_draft_version=cast(int, payload["expected_draft_version"]),
        root_plan_id=UUID(cast(str, payload["root_plan_id"])),
        source_revision=cast(int, payload["source_revision"]),
        expected_plan_version=cast(int, payload["expected_plan_version"]),
    )


class LoadScheduleApplicationContextHandler:
    step_type = StepType.LOAD_SCHEDULE_APPLICATION_CONTEXT
    version = "phase-6b-load-schedule-application-v1"

    def __init__(self, drafts: ScheduleDraftRepository, user: UserAccount) -> None:
        self._drafts = drafts
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.run.input_payload)
        try:
            draft = await self._drafts.get_draft(
                self._user.id, UUID(cast(str, payload["draft_id"]))
            )
            if draft is None:
                raise ValueError
            candidate = await self._drafts.get_candidate_set(self._user.id, draft.id)
            busy = await self._drafts.get_busy_snapshot(self._user.id, draft.id)
            if candidate is None or busy is None:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError(
                "Schedule application references could not be loaded."
            ) from exc
        output: JsonObject = {
            **payload,
            "candidate_set_id": str(candidate.id),
            "busy_snapshot_id": str(busy.id),
            "context_snapshot_reference_id": str(draft.context_snapshot_reference_id),
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(draft.id),
        )


class _PreviewScheduleHandler:
    version = "phase-6b-preview-schedule-application-v1"

    def __init__(
        self, service: SchedulePlanApplicationService, user: UserAccount
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
            raise PermanentStepError("Schedule application validation failed.") from exc
        output: JsonObject = {
            **payload,
            "application_fingerprint": preview.application_fingerprint,
            "calendar_revalidated": preview.validation.calendar_revalidated,
            "calendar_verification_status": (
                preview.validation.calendar_verification_status.value
            ),
            "safety_passed": preview.validation.passed,
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(draft_id),
        )


class ValidateScheduleApplicationHandler(_PreviewScheduleHandler):
    step_type = StepType.VALIDATE_SCHEDULE_APPLICATION
    version = "phase-6b-validate-schedule-application-v1"


class RevalidateCalendarBusyHandler(_PreviewScheduleHandler):
    step_type = StepType.REVALIDATE_CALENDAR_BUSY
    version = "phase-6b-revalidate-calendar-busy-v1"


class BuildSchedulePlanRevisionHandler:
    step_type = StepType.BUILD_SCHEDULE_PLAN_REVISION
    version = "phase-6b-build-schedule-plan-revision-v1"

    def __init__(
        self, service: SchedulePlanApplicationService, user: UserAccount
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
                "Schedule Plan Revision could not be created."
            ) from exc
        output: JsonObject = {
            **payload,
            "application_result_id": str(outcome.result.id),
            "created_revision": outcome.plan.revision,
            "created_plan_revision_id": str(outcome.plan.id),
            "resulting_plan_version": outcome.plan.version,
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(outcome.result.id),
        )


class VerifySchedulePlanSafetyHandler:
    step_type = StepType.VERIFY_SCHEDULE_PLAN_SAFETY
    version = "phase-6b-verify-schedule-plan-safety-v1"

    def __init__(
        self, service: SchedulePlanApplicationService, user: UserAccount
    ) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        try:
            outcome = await self._service.get_result(
                self._user, UUID(cast(str, payload["draft_id"]))
            )
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError("Schedule Revision verification failed.") from exc
        if outcome.plan.status is not WeeklyPlanStatus.VALIDATED:
            raise PermanentStepError("Schedule Revision is not awaiting confirmation.")
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload={**payload, "safety_passed": True},
            result_reference=str(outcome.result.id),
        )


class WaitForScheduleRevisionConfirmationHandler:
    step_type = StepType.WAIT_FOR_SCHEDULE_REVISION_CONFIRMATION
    version = "phase-6b-wait-schedule-confirmation-v1"

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        return StepExecutionResult(
            outcome=StepOutcome.WAITING_USER,
            output_payload=dict(context.claim.step.input_payload),
            result_reference=context.claim.run.result_reference,
        )


class FinalizeScheduleApplicationHandler:
    step_type = StepType.FINALIZE_SCHEDULE_APPLICATION
    version = "phase-6b-finalize-schedule-application-v1"

    def __init__(
        self,
        service: SchedulePlanApplicationService,
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
                self._user, UUID(cast(str, payload["draft_id"]))
            )
            root = UUID(cast(str, payload["root_plan_id"]))
            current = await self._plans.get_current_confirmed(root, self._user.id)
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError("Schedule application cannot finalize.") from exc
        if (
            outcome.draft.status is not ScheduleDraftStatus.APPLIED
            or outcome.plan.status is not WeeklyPlanStatus.CONFIRMED
            or current is None
            or current.id != outcome.plan.id
        ):
            raise PermanentStepError("Confirmed Schedule state is not finalizable.")
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=payload,
            result_reference=str(outcome.result.id),
        )
