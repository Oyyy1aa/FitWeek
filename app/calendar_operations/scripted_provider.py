"""In-memory idempotent Calendar provider used only by tests and local demos."""

import hashlib
from copy import deepcopy

from app.calendar_operations.gateway import CalendarProviderError
from app.domain.calendar_operations.models import (
    CalendarEventPayload,
    CalendarProviderResult,
)


class ScriptedCalendarWriteProvider:
    provider_name = "scripted"
    provider_version = "phase-6b-v1"

    def __init__(self) -> None:
        self.events: dict[str, CalendarEventPayload] = {}
        self._event_by_key: dict[str, str] = {}
        self._failures: list[CalendarProviderError] = []
        self._response_losses_after_create = 0
        self.calls: list[tuple[str, str]] = []

    def queue_failure(self, code: str, *, retryable: bool) -> None:
        self._failures.append(CalendarProviderError(code, retryable=retryable))

    def queue_response_loss_after_create(self) -> None:
        """Simulate a committed CREATE whose first response is lost."""

        self._response_losses_after_create += 1

    def _maybe_fail(self) -> None:
        if self._failures:
            raise self._failures.pop(0)

    async def create_event(
        self, *, calendar_id: str, operation_key: str, payload: CalendarEventPayload
    ) -> CalendarProviderResult:
        self.calls.append(("CREATE", operation_key))
        self._maybe_fail()
        external_id = self._event_by_key.get(operation_key)
        if external_id is None:
            external_id = (
                f"evt-{hashlib.sha256(operation_key.encode()).hexdigest()[:20]}"
            )
            self._event_by_key[operation_key] = external_id
        self.events[external_id] = deepcopy(payload)
        if self._response_losses_after_create:
            self._response_losses_after_create -= 1
            raise CalendarProviderError(
                "CALENDAR_PROVIDER_RESPONSE_LOST", retryable=True
            )
        return self._result(external_id, operation_key)

    async def update_event(
        self,
        *,
        calendar_id: str,
        external_event_id: str,
        operation_key: str,
        payload: CalendarEventPayload,
    ) -> CalendarProviderResult:
        self.calls.append(("UPDATE", operation_key))
        self._maybe_fail()
        if external_event_id not in self.events:
            raise CalendarProviderError("CALENDAR_EVENT_NOT_FOUND", retryable=False)
        self.events[external_event_id] = deepcopy(payload)
        return self._result(external_event_id, operation_key)

    async def delete_event(
        self,
        *,
        calendar_id: str,
        external_event_id: str,
        operation_key: str,
    ) -> CalendarProviderResult:
        self.calls.append(("DELETE", operation_key))
        self._maybe_fail()
        # DELETE is idempotent: absence means the requested final state already holds.
        self.events.pop(external_event_id, None)
        return self._result(external_event_id, operation_key)

    @staticmethod
    def _result(external_id: str, operation_key: str) -> CalendarProviderResult:
        reference = hashlib.sha256(
            f"{external_id}:{operation_key}".encode()
        ).hexdigest()
        return CalendarProviderResult(
            external_event_id=external_id,
            response_reference_hash=reference,
        )

    async def close(self) -> None:
        return None
