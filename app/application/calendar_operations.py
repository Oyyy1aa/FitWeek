"""Draft, review, and bounded execution of controlled Calendar operations."""

import hashlib
import json
from dataclasses import dataclass, replace
from uuid import NAMESPACE_URL, UUID, uuid5

from app.application.errors import (
    CalendarOperationDraftNotFound,
    CalendarOperationIdempotencyConflict,
    CalendarOperationPlanNotConfirmed,
    CalendarOperationPlanNotCurrent,
    CalendarOperationStateConflict,
    CalendarOperationVersionConflict,
    CalendarWriteDisabled,
)
from app.calendar_operations.gateway import CalendarGatewayResult, CalendarWriteGateway
from app.calendar_operations.reconciliation import CalendarReconciliationPolicy
from app.domain.calendar_operations.enums import (
    CalendarBindingStatus,
    CalendarOperationDraftStatus,
    CalendarOperationItemStatus,
    CalendarOperationType,
)
from app.domain.calendar_operations.models import (
    CalendarEventBinding,
    CalendarOperationAttempt,
    CalendarOperationDraft,
    CalendarOperationItem,
)
from app.domain.calendar_operations.protocols import CalendarOperationRepository
from app.domain.common import (
    RepositoryConflictError,
    RepositoryUniqueError,
    require_non_blank,
)
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.users.models import UserAccount
from app.orchestration.clock import Clock


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateCalendarOperationCommand:
    client_request_id: str
    expected_plan_version: int
    provider: str
    calendar_id: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.client_request_id, "client_request_id"),
            (self.provider, "provider"),
            (self.calendar_id, "calendar_id"),
        ):
            require_non_blank(value, name)
        if self.expected_plan_version < 1:
            raise ValueError("expected_plan_version must be positive")


class CalendarOperationService:
    policy_version = "calendar-operation-service-v1"

    def __init__(
        self,
        *,
        plans: PlanRepository,
        operations: CalendarOperationRepository,
        gateway: CalendarWriteGateway,
        clock: Clock,
        max_attempts: int = 3,
        policy: CalendarReconciliationPolicy | None = None,
    ) -> None:
        self._plans = plans
        self._operations = operations
        self._gateway = gateway
        self._clock = clock
        self._max_attempts = max_attempts
        self._policy = policy or CalendarReconciliationPolicy()

    async def create_draft(
        self,
        user: UserAccount,
        root_plan_id: UUID,
        revision: int,
        command: CreateCalendarOperationCommand,
    ) -> tuple[CalendarOperationDraft, bool]:
        plan = await self._plans.get_revision_for_user(root_plan_id, user.id, revision)
        if plan is None or plan.status is not WeeklyPlanStatus.CONFIRMED:
            raise CalendarOperationPlanNotConfirmed(
                "Calendar operations require a confirmed Plan Revision."
            )
        current = await self._plans.get_current_confirmed(root_plan_id, user.id)
        if current is None or current.id != plan.id:
            raise CalendarOperationPlanNotCurrent(
                "Calendar operations require the current Plan Revision."
            )
        if plan.version != command.expected_plan_version:
            raise CalendarOperationPlanNotCurrent("The Plan version is stale.")
        fingerprint = self._request_fingerprint(user.id, plan.id, command)
        existing = await self._operations.get_by_request(
            user.id, command.client_request_id
        )
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise CalendarOperationIdempotencyConflict(
                    "The Calendar request ID was used with different inputs."
                )
            return existing, False
        now = self._clock.now()
        draft_id = uuid5(
            NAMESPACE_URL,
            f"fitweek:calendar-draft:{user.id}:{command.client_request_id}",
        )
        bindings = await self._operations.list_bindings(
            user.id, command.provider, command.calendar_id, root_plan_id
        )
        items = self._policy.build_items(
            user_id=user.id,
            plan=plan,
            provider=command.provider,
            calendar_id=command.calendar_id,
            timezone=user.timezone,
            bindings=bindings,
            now=now,
            draft_seed=str(draft_id),
        )
        draft = CalendarOperationDraft(
            id=draft_id,
            user_id=user.id,
            client_request_id=command.client_request_id,
            request_fingerprint=fingerprint,
            provider=command.provider,
            calendar_id=command.calendar_id,
            root_plan_id=root_plan_id,
            revision=revision,
            plan_version=plan.version,
            items=items,
            status=CalendarOperationDraftStatus.PENDING_REVIEW,
            created_at=now,
            updated_at=now,
        )
        try:
            return await self._operations.save_draft(draft), True
        except RepositoryUniqueError as exc:
            raise CalendarOperationIdempotencyConflict(
                "The Calendar operation request conflicts with an existing request."
            ) from exc

    async def get_draft(
        self, user: UserAccount, draft_id: UUID
    ) -> CalendarOperationDraft:
        draft = await self._operations.get_draft(user.id, draft_id)
        if draft is None:
            raise CalendarOperationDraftNotFound(
                "The Calendar operation Draft was not found."
            )
        return draft

    async def approve(
        self, user: UserAccount, draft_id: UUID, expected_version: int
    ) -> CalendarOperationDraft:
        draft = await self.get_draft(user, draft_id)
        if draft.version != expected_version:
            raise CalendarOperationVersionConflict(
                "The Calendar Draft version is stale."
            )
        try:
            return await self._operations.update_draft(draft.approve(self._clock.now()))
        except RepositoryConflictError as exc:
            raise CalendarOperationVersionConflict(
                "The Calendar Draft changed concurrently."
            ) from exc

    async def reject(
        self, user: UserAccount, draft_id: UUID, expected_version: int
    ) -> CalendarOperationDraft:
        draft = await self.get_draft(user, draft_id)
        if draft.version != expected_version:
            raise CalendarOperationVersionConflict(
                "The Calendar Draft version is stale."
            )
        try:
            return await self._operations.update_draft(draft.reject(self._clock.now()))
        except RepositoryConflictError as exc:
            raise CalendarOperationVersionConflict(
                "The Calendar Draft changed concurrently."
            ) from exc

    async def execute(
        self,
        user: UserAccount,
        draft_id: UUID,
        *,
        correlation_id: UUID | None = None,
        run_id: UUID | None = None,
        step_id: UUID | None = None,
    ) -> CalendarOperationDraft:
        draft = await self.get_draft(user, draft_id)
        if self._gateway.provider_name == "none":
            raise CalendarWriteDisabled("Calendar write is disabled.")
        if draft.status is CalendarOperationDraftStatus.SUCCEEDED:
            return draft
        if draft.status not in {
            CalendarOperationDraftStatus.APPROVED,
            CalendarOperationDraftStatus.PARTIALLY_SUCCEEDED,
            CalendarOperationDraftStatus.EXECUTING,
        }:
            raise CalendarOperationStateConflict(
                "Only an approved, executing, or partially succeeded Calendar Draft "
                "can execute."
            )
        if draft.status is not CalendarOperationDraftStatus.EXECUTING:
            draft = await self._set_draft_status(
                draft, CalendarOperationDraftStatus.EXECUTING
            )
        for item in draft.items:
            if item.status in {
                CalendarOperationItemStatus.SUCCEEDED,
                CalendarOperationItemStatus.SKIPPED,
                CalendarOperationItemStatus.FAILED_PERMANENT,
            }:
                continue
            if item.attempt_count >= self._max_attempts:
                continue
            draft = await self._run_item(
                user,
                draft,
                item.id,
                correlation_id=correlation_id,
                run_id=run_id,
                step_id=step_id,
            )
        final = self._final_status(draft.items)
        return await self._set_draft_status(draft, final)

    async def attempts(
        self, user: UserAccount, draft_id: UUID
    ) -> tuple[CalendarOperationAttempt, ...]:
        await self.get_draft(user, draft_id)
        return await self._operations.list_attempts(user.id, draft_id)

    async def bindings(
        self,
        user: UserAccount,
        *,
        provider: str,
        calendar_id: str,
        root_plan_id: UUID,
    ) -> tuple[CalendarEventBinding, ...]:
        return await self._operations.list_bindings(
            user.id, provider, calendar_id, root_plan_id
        )

    async def _run_item(
        self,
        user: UserAccount,
        draft: CalendarOperationDraft,
        item_id: UUID,
        *,
        correlation_id: UUID | None,
        run_id: UUID | None,
        step_id: UUID | None,
    ) -> CalendarOperationDraft:
        item = next(item for item in draft.items if item.id == item_id)
        running = replace(item, status=CalendarOperationItemStatus.RUNNING)
        draft = await self._replace_item(draft, running)
        binding = (
            await self._operations.get_binding(user.id, item.binding_id)
            if item.binding_id is not None
            else None
        )
        result = await self._invoke(
            draft,
            item,
            binding,
            correlation_id=correlation_id,
            run_id=run_id,
            step_id=step_id,
        )
        await self._record_attempts(user, draft, item, result)
        # Item execution attempts are a business recovery counter. Gateway HTTP
        # attempts are retained separately in CalendarOperationAttempt records.
        # Circuit/bulkhead/deadline admission can be retryable without reaching
        # the gateway or Provider.  That is not a business execution attempt.
        attempt_count = item.attempt_count + (1 if result.attempts else 0)
        error_code = result.attempts[-1].error_code if result.attempts else None
        if result.succeeded and result.provider_result is not None:
            await self._commit_binding(user, draft, item, binding, result)
            updated = replace(
                item,
                status=CalendarOperationItemStatus.SUCCEEDED,
                attempt_count=attempt_count,
                last_error_code=None,
            )
        else:
            updated = replace(
                item,
                status=(
                    CalendarOperationItemStatus.FAILED_RETRYABLE
                    if result.retryable and attempt_count < self._max_attempts
                    else CalendarOperationItemStatus.FAILED_PERMANENT
                ),
                attempt_count=attempt_count,
                last_error_code=error_code,
            )
        return await self._replace_item(draft, updated)

    async def _invoke(
        self,
        draft: CalendarOperationDraft,
        item: CalendarOperationItem,
        binding: CalendarEventBinding | None,
        *,
        correlation_id: UUID | None,
        run_id: UUID | None,
        step_id: UUID | None,
    ) -> CalendarGatewayResult:
        if item.operation_type is CalendarOperationType.CREATE:
            assert item.payload is not None
            return await self._gateway.create(
                calendar_id=draft.calendar_id,
                operation_key=item.operation_key,
                payload=item.payload,
                correlation_id=correlation_id,
                run_id=run_id,
                step_id=step_id,
            )
        if binding is None:
            raise CalendarOperationStateConflict("Calendar binding was not found.")
        if item.operation_type is CalendarOperationType.UPDATE:
            assert item.payload is not None
            return await self._gateway.update(
                calendar_id=draft.calendar_id,
                external_event_id=binding.external_event_id,
                operation_key=item.operation_key,
                payload=item.payload,
                correlation_id=correlation_id,
                run_id=run_id,
                step_id=step_id,
            )
        return await self._gateway.delete(
            calendar_id=draft.calendar_id,
            external_event_id=binding.external_event_id,
            operation_key=item.operation_key,
            correlation_id=correlation_id,
            run_id=run_id,
            step_id=step_id,
        )

    async def _commit_binding(
        self,
        user: UserAccount,
        draft: CalendarOperationDraft,
        item: CalendarOperationItem,
        binding: CalendarEventBinding | None,
        gateway_result: CalendarGatewayResult,
    ) -> None:
        provider_result = gateway_result.provider_result
        assert provider_result is not None
        now = self._clock.now()
        if item.operation_type is CalendarOperationType.CREATE:
            assert item.payload is not None
            assert provider_result.external_event_id is not None
            binding = CalendarEventBinding(
                id=uuid5(
                    NAMESPACE_URL,
                    f"fitweek:binding:{user.id}:{draft.provider}:"
                    f"{draft.calendar_id}:{draft.root_plan_id}:{item.session_id}",
                ),
                user_id=user.id,
                provider=draft.provider,
                calendar_id=draft.calendar_id,
                root_plan_id=draft.root_plan_id,
                session_id=item.session_id,
                external_event_id=provider_result.external_event_id,
                stable_uid=item.payload.stable_uid,
                last_payload_fingerprint=item.payload.payload_fingerprint,
                status=CalendarBindingStatus.ACTIVE,
                created_at=now,
                updated_at=now,
                version=1,
            )
        elif (
            binding is not None and item.operation_type is CalendarOperationType.UPDATE
        ):
            assert item.payload is not None
            binding = replace(
                binding,
                last_payload_fingerprint=item.payload.payload_fingerprint,
                updated_at=now,
                version=binding.version + 1,
            )
        elif binding is not None:
            binding = replace(
                binding,
                status=CalendarBindingStatus.DELETED,
                updated_at=now,
                version=binding.version + 1,
            )
        assert binding is not None
        await self._operations.save_binding(binding)

    async def _record_attempts(
        self,
        user: UserAccount,
        draft: CalendarOperationDraft,
        item: CalendarOperationItem,
        result: CalendarGatewayResult,
    ) -> None:
        now = self._clock.now()
        existing = await self._operations.list_attempts(user.id, draft.id)
        next_attempt_no = len(existing) + 1
        for trace in result.attempts:
            attempt_no = next_attempt_no + trace.attempt_no - 1
            await self._operations.save_attempt(
                CalendarOperationAttempt(
                    id=uuid5(
                        NAMESPACE_URL,
                        f"fitweek:calendar-attempt:{draft.id}:{item.id}:{attempt_no}",
                    ),
                    user_id=user.id,
                    draft_id=draft.id,
                    item_id=item.id,
                    attempt_no=attempt_no,
                    outcome=trace.outcome,
                    error_code=trace.error_code,
                    response_reference_hash=trace.response_reference_hash,
                    started_at=now,
                    finished_at=now,
                )
            )

    async def _replace_item(
        self, draft: CalendarOperationDraft, item: CalendarOperationItem
    ) -> CalendarOperationDraft:
        updated = replace(
            draft,
            items=tuple(
                item if value.id == item.id else value for value in draft.items
            ),
            updated_at=self._clock.now(),
            version=draft.version + 1,
        )
        return await self._operations.update_draft(updated)

    async def _set_draft_status(
        self,
        draft: CalendarOperationDraft,
        status: CalendarOperationDraftStatus,
    ) -> CalendarOperationDraft:
        updated = replace(
            draft,
            status=status,
            updated_at=self._clock.now(),
            version=draft.version + 1,
        )
        return await self._operations.update_draft(updated)

    @staticmethod
    def _final_status(
        items: tuple[CalendarOperationItem, ...],
    ) -> CalendarOperationDraftStatus:
        failures = tuple(
            item
            for item in items
            if item.status
            in {
                CalendarOperationItemStatus.FAILED_RETRYABLE,
                CalendarOperationItemStatus.FAILED_PERMANENT,
            }
        )
        if not failures:
            return CalendarOperationDraftStatus.SUCCEEDED
        if any(
            item.status is CalendarOperationItemStatus.FAILED_RETRYABLE
            for item in items
        ) or any(
            item.status is CalendarOperationItemStatus.SUCCEEDED for item in items
        ):
            return CalendarOperationDraftStatus.PARTIALLY_SUCCEEDED
        return CalendarOperationDraftStatus.FAILED_PERMANENT

    @classmethod
    def _request_fingerprint(
        cls, user_id: UUID, plan_id: UUID, command: CreateCalendarOperationCommand
    ) -> str:
        payload = {
            "user_id": str(user_id),
            "plan_id": str(plan_id),
            "plan_version": command.expected_plan_version,
            "provider": command.provider,
            "calendar_id": command.calendar_id,
            "policy": cls.policy_version,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
