"""Provider and repository ports for controlled Calendar side effects."""

from typing import Protocol
from uuid import UUID

from app.domain.calendar_operations.models import (
    CalendarEventBinding,
    CalendarEventPayload,
    CalendarOperationAttempt,
    CalendarOperationDraft,
    CalendarProviderResult,
)


class CalendarWriteProvider(Protocol):
    provider_name: str
    provider_version: str

    async def create_event(
        self, *, calendar_id: str, operation_key: str, payload: CalendarEventPayload
    ) -> CalendarProviderResult: ...

    async def update_event(
        self,
        *,
        calendar_id: str,
        external_event_id: str,
        operation_key: str,
        payload: CalendarEventPayload,
    ) -> CalendarProviderResult: ...

    async def delete_event(
        self,
        *,
        calendar_id: str,
        external_event_id: str,
        operation_key: str,
    ) -> CalendarProviderResult: ...

    async def close(self) -> None: ...


class CalendarOperationRepository(Protocol):
    async def get_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> CalendarOperationDraft | None: ...
    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> CalendarOperationDraft | None: ...
    async def save_draft(
        self, draft: CalendarOperationDraft
    ) -> CalendarOperationDraft: ...
    async def update_draft(
        self, draft: CalendarOperationDraft
    ) -> CalendarOperationDraft: ...
    async def list_bindings(
        self, user_id: UUID, provider: str, calendar_id: str, root_plan_id: UUID
    ) -> tuple[CalendarEventBinding, ...]: ...
    async def list_bindings_for_plan(
        self, user_id: UUID, root_plan_id: UUID
    ) -> tuple[CalendarEventBinding, ...]: ...
    async def get_binding(
        self, user_id: UUID, binding_id: UUID
    ) -> CalendarEventBinding | None: ...
    async def save_binding(
        self, binding: CalendarEventBinding
    ) -> CalendarEventBinding: ...
    async def save_attempt(
        self, attempt: CalendarOperationAttempt
    ) -> CalendarOperationAttempt: ...
    async def list_attempts(
        self, user_id: UUID, draft_id: UUID
    ) -> tuple[CalendarOperationAttempt, ...]: ...
