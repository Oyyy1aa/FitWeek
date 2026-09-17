"""Concurrency-safe memory adapter for Calendar operation control records."""

from copy import deepcopy
from uuid import UUID

from app.domain.calendar_operations.models import (
    CalendarEventBinding,
    CalendarOperationAttempt,
    CalendarOperationDraft,
)
from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.persistence.memory.store import InMemoryStore


class InMemoryCalendarOperationRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def get_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> CalendarOperationDraft | None:
        async with self._store.lock:
            value = self._store._calendar_operation_drafts.get(draft_id)
            return deepcopy(value) if value and value.user_id == user_id else None

    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> CalendarOperationDraft | None:
        async with self._store.lock:
            draft_id = self._store._calendar_operation_id_by_request.get(
                (user_id, client_request_id)
            )
            return (
                deepcopy(self._store._calendar_operation_drafts.get(draft_id))
                if draft_id
                else None
            )

    async def save_draft(self, draft: CalendarOperationDraft) -> CalendarOperationDraft:
        async with self._store.lock:
            key = (draft.user_id, draft.client_request_id)
            if (
                draft.id in self._store._calendar_operation_drafts
                or key in self._store._calendar_operation_id_by_request
            ):
                raise RepositoryUniqueError("calendar_operation.request", key)
            self._store._calendar_operation_drafts[draft.id] = deepcopy(draft)
            self._store._calendar_operation_id_by_request[key] = draft.id
            return deepcopy(draft)

    async def update_draft(
        self, draft: CalendarOperationDraft
    ) -> CalendarOperationDraft:
        async with self._store.lock:
            current = self._store._calendar_operation_drafts.get(draft.id)
            if current is None:
                raise RepositoryUniqueError("calendar_operation.id", draft.id)
            if draft.version != current.version + 1:
                raise RepositoryConflictError(
                    "CalendarOperationDraft",
                    draft.id,
                    expected_version=current.version + 1,
                    actual_version=draft.version,
                )
            self._store._calendar_operation_drafts[draft.id] = deepcopy(draft)
            return deepcopy(draft)

    async def list_bindings(
        self, user_id: UUID, provider: str, calendar_id: str, root_plan_id: UUID
    ) -> tuple[CalendarEventBinding, ...]:
        async with self._store.lock:
            values = (
                value
                for value in self._store._calendar_bindings.values()
                if value.user_id == user_id
                and value.provider == provider
                and value.calendar_id == calendar_id
                and value.root_plan_id == root_plan_id
            )
            return tuple(
                deepcopy(item)
                for item in sorted(values, key=lambda item: str(item.session_id))
            )

    async def get_binding(
        self, user_id: UUID, binding_id: UUID
    ) -> CalendarEventBinding | None:
        async with self._store.lock:
            value = self._store._calendar_bindings.get(binding_id)
            return deepcopy(value) if value and value.user_id == user_id else None

    async def list_bindings_for_plan(
        self, user_id: UUID, root_plan_id: UUID
    ) -> tuple[CalendarEventBinding, ...]:
        """Return safe Plan binding snapshots for read-only impact analysis."""

        async with self._store.lock:
            values = (
                value
                for value in self._store._calendar_bindings.values()
                if value.user_id == user_id and value.root_plan_id == root_plan_id
            )
            return tuple(
                deepcopy(item)
                for item in sorted(values, key=lambda item: str(item.session_id))
            )

    async def save_binding(self, binding: CalendarEventBinding) -> CalendarEventBinding:
        async with self._store.lock:
            key = (
                binding.user_id,
                binding.provider,
                binding.calendar_id,
                binding.root_plan_id,
                binding.session_id,
            )
            existing_id = self._store._calendar_binding_id_by_key.get(key)
            current = self._store._calendar_bindings.get(binding.id)
            if current is None:
                if existing_id is not None:
                    raise RepositoryUniqueError("calendar_binding.key", key)
                self._store._calendar_binding_id_by_key[key] = binding.id
            elif binding.version != current.version + 1:
                raise RepositoryConflictError(
                    "CalendarEventBinding",
                    binding.id,
                    expected_version=current.version + 1,
                    actual_version=binding.version,
                )
            self._store._calendar_bindings[binding.id] = deepcopy(binding)
            return deepcopy(binding)

    async def save_attempt(
        self, attempt: CalendarOperationAttempt
    ) -> CalendarOperationAttempt:
        async with self._store.lock:
            if attempt.id in self._store._calendar_operation_attempts:
                raise RepositoryUniqueError("calendar_attempt.id", attempt.id)
            self._store._calendar_operation_attempts[attempt.id] = deepcopy(attempt)
            return deepcopy(attempt)

    async def list_attempts(
        self, user_id: UUID, draft_id: UUID
    ) -> tuple[CalendarOperationAttempt, ...]:
        async with self._store.lock:
            values = (
                value
                for value in self._store._calendar_operation_attempts.values()
                if value.user_id == user_id and value.draft_id == draft_id
            )
            return tuple(
                deepcopy(item)
                for item in sorted(
                    values,
                    key=lambda item: (str(item.item_id), item.attempt_no),
                )
            )

    async def clear(self) -> None:
        async with self._store.lock:
            self._store._calendar_operation_drafts.clear()
            self._store._calendar_operation_id_by_request.clear()
            self._store._calendar_bindings.clear()
            self._store._calendar_binding_id_by_key.clear()
            self._store._calendar_operation_attempts.clear()
