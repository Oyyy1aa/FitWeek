"""Handlers for approved Calendar operation execution."""

from typing import cast
from uuid import UUID

from app.application.calendar_operations import CalendarOperationService
from app.application.errors import ApplicationError
from app.domain.calendar_operations.enums import CalendarOperationDraftStatus
from app.domain.orchestration.enums import StepOutcome, StepType
from app.domain.orchestration.models import JsonObject
from app.domain.users.models import UserAccount
from app.orchestration.handler import (
    PermanentStepError,
    RetryableStepError,
    StepExecutionContext,
    StepExecutionResult,
)


class LoadCalendarOperationDraftHandler:
    step_type = StepType.LOAD_CALENDAR_OPERATION_DRAFT
    version = "phase-6b-load-calendar-operation-v1"

    def __init__(self, service: CalendarOperationService, user: UserAccount) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.run.input_payload)
        try:
            draft = await self._service.get_draft(
                self._user, UUID(cast(str, payload["draft_id"]))
            )
        except (ApplicationError, KeyError, TypeError, ValueError) as exc:
            raise PermanentStepError("Calendar Draft could not be loaded.") from exc
        output: JsonObject = {
            "draft_id": str(draft.id),
            "draft_version": draft.version,
            "root_plan_id": str(draft.root_plan_id),
            "revision": draft.revision,
            "item_ids": [str(item.id) for item in draft.items],
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(draft.id),
        )


class ValidateCalendarOperationApprovalHandler:
    step_type = StepType.VALIDATE_CALENDAR_OPERATION_APPROVAL
    version = "phase-6b-validate-calendar-approval-v1"

    def __init__(self, service: CalendarOperationService, user: UserAccount) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        draft = await self._service.get_draft(
            self._user, UUID(cast(str, payload["draft_id"]))
        )
        if draft.status not in {
            CalendarOperationDraftStatus.APPROVED,
            CalendarOperationDraftStatus.PARTIALLY_SUCCEEDED,
        }:
            raise PermanentStepError("Calendar Draft has not been approved.")
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=payload,
            result_reference=str(draft.id),
        )


class ExecuteCalendarOperationItemsHandler:
    step_type = StepType.EXECUTE_CALENDAR_OPERATION_ITEMS
    version = "phase-6b-execute-calendar-items-v1"

    def __init__(self, service: CalendarOperationService, user: UserAccount) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        try:
            draft = await self._service.execute(
                self._user,
                UUID(cast(str, payload["draft_id"])),
                correlation_id=context.claim.run.id,
                run_id=context.claim.run.id,
                step_id=context.claim.step.id,
            )
        except ApplicationError as exc:
            raise PermanentStepError("Calendar execution was rejected.") from exc
        output: JsonObject = {
            **payload,
            "draft_version": draft.version,
            "draft_status": draft.status.value,
            "succeeded_item_ids": [
                str(item.id)
                for item in draft.items
                if item.status.value in {"SUCCEEDED", "SKIPPED"}
            ],
        }
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=output,
            result_reference=str(draft.id),
        )


class VerifyCalendarOperationResultsHandler:
    step_type = StepType.VERIFY_CALENDAR_OPERATION_RESULTS
    version = "phase-6b-verify-calendar-results-v1"

    def __init__(self, service: CalendarOperationService, user: UserAccount) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        draft = await self._service.get_draft(
            self._user, UUID(cast(str, payload["draft_id"]))
        )
        if draft.status is CalendarOperationDraftStatus.PARTIALLY_SUCCEEDED:
            # Resume only retryable items; the executor skips already successful
            # and permanently failed items. This keeps recovery idempotent.
            draft = await self._service.execute(
                self._user,
                draft.id,
                correlation_id=context.claim.run.id,
                run_id=context.claim.run.id,
                step_id=context.claim.step.id,
            )
        if draft.status is CalendarOperationDraftStatus.PARTIALLY_SUCCEEDED:
            raise RetryableStepError("Calendar operation has retryable items.")
        if draft.status is not CalendarOperationDraftStatus.SUCCEEDED:
            raise PermanentStepError("Calendar operation did not fully succeed.")
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload={**payload, "draft_status": draft.status.value},
            result_reference=str(draft.id),
        )


class FinalizeCalendarOperationHandler:
    step_type = StepType.FINALIZE_CALENDAR_OPERATION
    version = "phase-6b-finalize-calendar-operation-v1"

    def __init__(self, service: CalendarOperationService, user: UserAccount) -> None:
        self._service = service
        self._user = user

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = dict(context.claim.step.input_payload)
        draft = await self._service.get_draft(
            self._user, UUID(cast(str, payload["draft_id"]))
        )
        if draft.status is not CalendarOperationDraftStatus.SUCCEEDED:
            raise PermanentStepError("Calendar operation cannot finalize.")
        return StepExecutionResult(
            outcome=StepOutcome.SUCCEEDED,
            output_payload=payload,
            result_reference=str(draft.id),
        )
