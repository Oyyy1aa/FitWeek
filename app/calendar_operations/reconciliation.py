"""Pure deterministic reconciliation of one confirmed Plan and its bindings."""

import hashlib
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from app.calendar_operations.payload_policy import CalendarPayloadPolicy
from app.domain.calendar_operations.enums import (
    CalendarBindingStatus,
    CalendarOperationItemStatus,
    CalendarOperationType,
)
from app.domain.calendar_operations.models import (
    CalendarEventBinding,
    CalendarOperationItem,
)
from app.domain.plans.models import WeeklyPlan
from app.domain.sessions.models import WorkoutSessionStatus


class CalendarReconciliationPolicy:
    version = "calendar-reconciliation-v1"

    def __init__(self, payloads: CalendarPayloadPolicy | None = None) -> None:
        self._payloads = payloads or CalendarPayloadPolicy()

    def build_items(
        self,
        *,
        user_id: UUID,
        plan: WeeklyPlan,
        provider: str,
        calendar_id: str,
        timezone: str,
        bindings: tuple[CalendarEventBinding, ...],
        now: datetime,
        draft_seed: str,
    ) -> tuple[CalendarOperationItem, ...]:
        active = {
            binding.session_id: binding
            for binding in bindings
            if binding.status is CalendarBindingStatus.ACTIVE
        }
        sessions = {
            session.id: session
            for session in plan.sessions
            if session.status is not WorkoutSessionStatus.CANCELLED
            and session.scheduled_end > now
        }
        items: list[CalendarOperationItem] = []
        for session in sorted(
            sessions.values(), key=lambda item: (item.scheduled_start, str(item.id))
        ):
            payload = self._payloads.build(
                user_id=user_id, plan=plan, session=session, timezone=timezone
            )
            binding = active.get(session.id)
            if binding is None:
                operation_type = CalendarOperationType.CREATE
            elif binding.last_payload_fingerprint == payload.payload_fingerprint:
                operation_type = CalendarOperationType.KEEP
            else:
                operation_type = CalendarOperationType.UPDATE
            items.append(
                self._item(
                    draft_seed=draft_seed,
                    provider=provider,
                    calendar_id=calendar_id,
                    revision=plan.revision,
                    operation_type=operation_type,
                    session_id=session.id,
                    payload_fingerprint=payload.payload_fingerprint,
                    payload=payload,
                    binding_id=None if binding is None else binding.id,
                )
            )
        for session_id, binding in sorted(
            active.items(), key=lambda item: str(item[0])
        ):
            if session_id in sessions:
                continue
            items.append(
                self._item(
                    draft_seed=draft_seed,
                    provider=provider,
                    calendar_id=calendar_id,
                    revision=plan.revision,
                    operation_type=CalendarOperationType.DELETE,
                    session_id=session_id,
                    payload_fingerprint=binding.last_payload_fingerprint,
                    payload=None,
                    binding_id=binding.id,
                )
            )
        return tuple(items)

    def _item(
        self,
        *,
        draft_seed: str,
        provider: str,
        calendar_id: str,
        revision: int,
        operation_type: CalendarOperationType,
        session_id: UUID,
        payload_fingerprint: str,
        payload: object,
        binding_id: UUID | None,
    ) -> CalendarOperationItem:
        raw_key = (
            f"{provider}:{calendar_id}:{session_id}:{revision}:"
            f"{operation_type.value}:{payload_fingerprint}:{self.version}"
        )
        operation_key = hashlib.sha256(raw_key.encode()).hexdigest()
        item_id = uuid5(NAMESPACE_URL, f"fitweek:calendar-item:{draft_seed}:{raw_key}")
        from app.domain.calendar_operations.models import CalendarEventPayload

        typed_payload = payload if isinstance(payload, CalendarEventPayload) else None
        return CalendarOperationItem(
            id=item_id,
            operation_type=operation_type,
            session_id=session_id,
            operation_key=operation_key,
            payload=typed_payload,
            binding_id=binding_id,
            status=(
                CalendarOperationItemStatus.SKIPPED
                if operation_type is CalendarOperationType.KEEP
                else CalendarOperationItemStatus.PENDING
            ),
        )
