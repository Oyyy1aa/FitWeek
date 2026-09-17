"""MySQL persistence for immutable Calendar operation control facts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.calendar_operations.enums import (
    CalendarAttemptOutcome,
    CalendarBindingStatus,
    CalendarOperationDraftStatus,
    CalendarOperationItemStatus,
    CalendarOperationType,
)
from app.domain.calendar_operations.models import (
    CalendarEventBinding,
    CalendarEventPayload,
    CalendarOperationAttempt,
    CalendarOperationDraft,
    CalendarOperationItem,
)
from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.persistence.mysql.models import (
    CalendarEventBindingModel,
    CalendarOperationAttemptModel,
    CalendarOperationDraftModel,
    CalendarOperationItemModel,
)

_SAVE_DRAFT_LOCK_ATTEMPTS = 3


class MySQLCalendarOperationRepository:
    """Store normalized Calendar Draft aggregates and write-control facts."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> CalendarOperationDraft | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(CalendarOperationDraftModel).where(
                    CalendarOperationDraftModel.id == str(draft_id),
                    CalendarOperationDraftModel.user_id == str(user_id),
                )
            )
            return None if row is None else await self._draft_from_row(session, row)

    async def get_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> CalendarOperationDraft | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(CalendarOperationDraftModel).where(
                    CalendarOperationDraftModel.user_id == str(user_id),
                    CalendarOperationDraftModel.client_request_id == client_request_id,
                )
            )
            return None if row is None else await self._draft_from_row(session, row)

    async def save_draft(self, draft: CalendarOperationDraft) -> CalendarOperationDraft:
        request_key = (draft.user_id, draft.client_request_id)
        try:
            for attempt in range(_SAVE_DRAFT_LOCK_ATTEMPTS):
                try:
                    return await self._save_draft_once(draft, request_key)
                except OperationalError as exc:
                    if (
                        not self._is_retryable_lock_error(exc)
                        or attempt == _SAVE_DRAFT_LOCK_ATTEMPTS - 1
                    ):
                        raise
        except IntegrityError as exc:
            existing_draft = await self.get_by_request(
                draft.user_id, draft.client_request_id
            )
            if existing_draft is not None:
                if existing_draft.request_fingerprint == draft.request_fingerprint:
                    return existing_draft
                raise RepositoryUniqueError(
                    "calendar_operation.request", request_key
                ) from exc
            raise RepositoryUniqueError(
                "calendar_operation.request", request_key
            ) from exc
        raise RuntimeError("Bounded Calendar Draft save retry exhausted unexpectedly.")

    async def _save_draft_once(
        self,
        draft: CalendarOperationDraft,
        request_key: tuple[UUID, str],
    ) -> CalendarOperationDraft:
        async with self._sessions() as session:
            async with session.begin():
                existing_row = await session.scalar(
                    select(CalendarOperationDraftModel)
                    .where(
                        CalendarOperationDraftModel.user_id == str(draft.user_id),
                        CalendarOperationDraftModel.client_request_id
                        == draft.client_request_id,
                    )
                    .with_for_update()
                )
                if existing_row is not None:
                    return await self._existing_draft(
                        session, existing_row, draft, request_key
                    )
                collision = await session.get(
                    CalendarOperationDraftModel, str(draft.id)
                )
                if collision is not None:
                    raise RepositoryUniqueError("calendar_operation.id", draft.id)
                session.add(self._draft_row(draft))
                session.add_all(self._item_rows(draft))
                await session.flush()
                return draft

    async def update_draft(
        self, draft: CalendarOperationDraft
    ) -> CalendarOperationDraft:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(CalendarOperationDraftModel)
                    .where(
                        CalendarOperationDraftModel.id == str(draft.id),
                        CalendarOperationDraftModel.user_id == str(draft.user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    raise RepositoryUniqueError("calendar_operation.id", draft.id)
                current = await self._draft_from_row(session, row, for_update=True)
                expected = current.version + 1
                if draft.version != expected:
                    raise RepositoryConflictError(
                        "CalendarOperationDraft",
                        draft.id,
                        expected_version=expected,
                        actual_version=draft.version,
                    )
                self._ensure_immutable_draft(current, draft)
                self._apply_draft(row, draft)
                stored_items = (
                    await session.scalars(
                        select(CalendarOperationItemModel)
                        .where(CalendarOperationItemModel.draft_id == row.id)
                        .order_by(CalendarOperationItemModel.sequence_no)
                        .with_for_update()
                    )
                ).all()
                for stored, incoming in zip(stored_items, draft.items, strict=True):
                    stored.status = incoming.status.value
                    stored.attempt_count = incoming.attempt_count
                    stored.last_error_code = incoming.last_error_code
            return draft

    async def list_bindings(
        self, user_id: UUID, provider: str, calendar_id: str, root_plan_id: UUID
    ) -> tuple[CalendarEventBinding, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CalendarEventBindingModel)
                    .where(
                        CalendarEventBindingModel.user_id == str(user_id),
                        CalendarEventBindingModel.provider == provider,
                        CalendarEventBindingModel.calendar_id == calendar_id,
                        CalendarEventBindingModel.root_plan_id == str(root_plan_id),
                    )
                    .order_by(CalendarEventBindingModel.session_id)
                )
            ).all()
            return tuple(self._binding_from_row(row) for row in rows)

    async def list_bindings_for_plan(
        self, user_id: UUID, root_plan_id: UUID
    ) -> tuple[CalendarEventBinding, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CalendarEventBindingModel)
                    .where(
                        CalendarEventBindingModel.user_id == str(user_id),
                        CalendarEventBindingModel.root_plan_id == str(root_plan_id),
                    )
                    .order_by(CalendarEventBindingModel.session_id)
                )
            ).all()
            return tuple(self._binding_from_row(row) for row in rows)

    async def get_binding(
        self, user_id: UUID, binding_id: UUID
    ) -> CalendarEventBinding | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(CalendarEventBindingModel).where(
                    CalendarEventBindingModel.id == str(binding_id),
                    CalendarEventBindingModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._binding_from_row(row)

    async def save_binding(self, binding: CalendarEventBinding) -> CalendarEventBinding:
        logical_key = self._binding_key(binding)
        try:
            async with self._sessions() as session:
                async with session.begin():
                    current = await session.get(
                        CalendarEventBindingModel, str(binding.id), with_for_update=True
                    )
                    if current is None:
                        existing = await session.scalar(
                            select(CalendarEventBindingModel)
                            .where(
                                CalendarEventBindingModel.user_id
                                == str(binding.user_id),
                                CalendarEventBindingModel.provider == binding.provider,
                                CalendarEventBindingModel.calendar_id
                                == binding.calendar_id,
                                CalendarEventBindingModel.root_plan_id
                                == str(binding.root_plan_id),
                                CalendarEventBindingModel.session_id
                                == str(binding.session_id),
                            )
                            .with_for_update()
                        )
                        if existing is not None:
                            raise RepositoryUniqueError(
                                "calendar_binding.key", logical_key
                            )
                        session.add(self._binding_row(binding))
                    else:
                        current_value = self._binding_from_row(current)
                        if current_value.user_id != binding.user_id:
                            raise RepositoryUniqueError(
                                "calendar_binding.id", binding.id
                            )
                        expected = current.version + 1
                        if binding.version != expected:
                            raise RepositoryConflictError(
                                "CalendarEventBinding",
                                binding.id,
                                expected_version=expected,
                                actual_version=binding.version,
                            )
                        if self._binding_key(current_value) != logical_key:
                            raise RepositoryUniqueError(
                                "calendar_binding.key", logical_key
                            )
                        immutable_fields = (
                            "user_id",
                            "provider",
                            "calendar_id",
                            "root_plan_id",
                            "session_id",
                            "external_event_id",
                            "stable_uid",
                            "created_at",
                        )
                        if any(
                            getattr(current_value, field) != getattr(binding, field)
                            for field in immutable_fields
                        ):
                            raise RepositoryUniqueError(
                                "calendar_binding.immutable", binding.id
                            )
                        self._apply_binding(current, binding)
                    await session.flush()
                    return binding
        except IntegrityError as exc:
            raise RepositoryUniqueError("calendar_binding.key", logical_key) from exc

    async def save_attempt(
        self, attempt: CalendarOperationAttempt
    ) -> CalendarOperationAttempt:
        attempt_key = (attempt.item_id, attempt.attempt_no)
        try:
            async with self._sessions() as session:
                async with session.begin():
                    owner = await session.scalar(
                        select(CalendarOperationItemModel.id)
                        .join(
                            CalendarOperationDraftModel,
                            CalendarOperationDraftModel.id
                            == CalendarOperationItemModel.draft_id,
                        )
                        .where(
                            CalendarOperationItemModel.id == str(attempt.item_id),
                            CalendarOperationItemModel.draft_id
                            == str(attempt.draft_id),
                            CalendarOperationDraftModel.user_id == str(attempt.user_id),
                        )
                    )
                    if owner is None:
                        raise RepositoryUniqueError(
                            "calendar_attempt.owner", attempt.id
                        )
                    if await session.get(
                        CalendarOperationAttemptModel, str(attempt.id)
                    ):
                        raise RepositoryUniqueError("calendar_attempt.id", attempt.id)
                    existing = await session.scalar(
                        select(CalendarOperationAttemptModel.id).where(
                            CalendarOperationAttemptModel.item_id
                            == str(attempt.item_id),
                            CalendarOperationAttemptModel.attempt_no
                            == attempt.attempt_no,
                        )
                    )
                    if existing is not None:
                        raise RepositoryUniqueError(
                            "calendar_attempt.number", attempt_key
                        )
                    session.add(self._attempt_row(attempt))
                    await session.flush()
                    return attempt
        except IntegrityError as exc:
            raise RepositoryUniqueError("calendar_attempt.number", attempt_key) from exc

    async def list_attempts(
        self, user_id: UUID, draft_id: UUID
    ) -> tuple[CalendarOperationAttempt, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(CalendarOperationAttemptModel)
                    .join(
                        CalendarOperationDraftModel,
                        CalendarOperationDraftModel.id
                        == CalendarOperationAttemptModel.draft_id,
                    )
                    .where(
                        CalendarOperationAttemptModel.user_id == str(user_id),
                        CalendarOperationAttemptModel.draft_id == str(draft_id),
                        CalendarOperationDraftModel.user_id == str(user_id),
                    )
                    .order_by(
                        CalendarOperationAttemptModel.item_id,
                        CalendarOperationAttemptModel.attempt_no,
                    )
                )
            ).all()
            return tuple(self._attempt_from_row(row) for row in rows)

    @classmethod
    async def _existing_draft(
        cls,
        session: AsyncSession,
        row: CalendarOperationDraftModel,
        draft: CalendarOperationDraft,
        request_key: tuple[UUID, str],
    ) -> CalendarOperationDraft:
        if row.request_fingerprint != draft.request_fingerprint:
            raise RepositoryUniqueError("calendar_operation.request", request_key)
        return await cls._draft_from_row(session, row, for_update=True)

    @classmethod
    async def _draft_from_row(
        cls,
        session: AsyncSession,
        row: CalendarOperationDraftModel,
        *,
        for_update: bool = False,
    ) -> CalendarOperationDraft:
        query = (
            select(CalendarOperationItemModel)
            .where(CalendarOperationItemModel.draft_id == row.id)
            .order_by(CalendarOperationItemModel.sequence_no)
        )
        if for_update:
            query = query.with_for_update()
        items = (await session.scalars(query)).all()
        return CalendarOperationDraft(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            client_request_id=row.client_request_id,
            request_fingerprint=row.request_fingerprint,
            provider=row.provider,
            calendar_id=row.calendar_id,
            root_plan_id=UUID(row.root_plan_id),
            revision=row.revision,
            plan_version=row.plan_version,
            items=tuple(cls._item_from_row(item) for item in items),
            status=CalendarOperationDraftStatus(row.status),
            created_at=cls._utc(row.created_at),
            updated_at=cls._utc(row.updated_at),
            approved_at=None if row.approved_at is None else cls._utc(row.approved_at),
            rejected_at=None if row.rejected_at is None else cls._utc(row.rejected_at),
            version=row.version,
        )

    @classmethod
    def _draft_row(cls, value: CalendarOperationDraft) -> CalendarOperationDraftModel:
        return CalendarOperationDraftModel(
            id=str(value.id),
            user_id=str(value.user_id),
            client_request_id=value.client_request_id,
            request_fingerprint=value.request_fingerprint,
            provider=value.provider,
            calendar_id=value.calendar_id,
            root_plan_id=str(value.root_plan_id),
            revision=value.revision,
            plan_version=value.plan_version,
            status=value.status.value,
            created_at=cls._db_time(value.created_at),
            updated_at=cls._db_time(value.updated_at),
            approved_at=cls._optional_db_time(value.approved_at),
            rejected_at=cls._optional_db_time(value.rejected_at),
            version=value.version,
        )

    @classmethod
    def _apply_draft(
        cls, row: CalendarOperationDraftModel, value: CalendarOperationDraft
    ) -> None:
        for field in (
            "status",
            "updated_at",
            "approved_at",
            "rejected_at",
            "version",
        ):
            setattr(row, field, getattr(cls._draft_row(value), field))

    @classmethod
    def _item_rows(
        cls, draft: CalendarOperationDraft
    ) -> list[CalendarOperationItemModel]:
        return [
            cls._item_row(draft.id, sequence_no, item)
            for sequence_no, item in enumerate(draft.items, start=1)
        ]

    @classmethod
    def _item_row(
        cls, draft_id: UUID, sequence_no: int, value: CalendarOperationItem
    ) -> CalendarOperationItemModel:
        payload = value.payload
        return CalendarOperationItemModel(
            id=str(value.id),
            draft_id=str(draft_id),
            sequence_no=sequence_no,
            operation_type=value.operation_type.value,
            session_id=str(value.session_id),
            operation_key=value.operation_key,
            binding_id=None if value.binding_id is None else str(value.binding_id),
            stable_uid=None if payload is None else payload.stable_uid,
            summary=None if payload is None else payload.summary,
            description=None if payload is None else payload.description,
            start_at=None if payload is None else cls._db_time(payload.start),
            end_at=None if payload is None else cls._db_time(payload.end),
            timezone=None if payload is None else payload.timezone,
            transparency=None if payload is None else payload.transparency,
            payload_fingerprint=None
            if payload is None
            else payload.payload_fingerprint,
            status=value.status.value,
            attempt_count=value.attempt_count,
            last_error_code=value.last_error_code,
        )

    @classmethod
    def _item_from_row(cls, row: CalendarOperationItemModel) -> CalendarOperationItem:
        payload = None
        if row.stable_uid is not None:
            assert row.start_at is not None
            assert row.end_at is not None
            assert row.timezone is not None
            assert row.transparency is not None
            assert row.payload_fingerprint is not None
            assert row.summary is not None
            payload = CalendarEventPayload(
                session_id=UUID(row.session_id),
                stable_uid=row.stable_uid,
                summary=row.summary,
                description=row.description or "",
                start=cls._utc(row.start_at),
                end=cls._utc(row.end_at),
                timezone=row.timezone,
                transparency=row.transparency,
                payload_fingerprint=row.payload_fingerprint,
            )
        return CalendarOperationItem(
            id=UUID(row.id),
            operation_type=CalendarOperationType(row.operation_type),
            session_id=UUID(row.session_id),
            operation_key=row.operation_key,
            payload=payload,
            binding_id=None if row.binding_id is None else UUID(row.binding_id),
            status=CalendarOperationItemStatus(row.status),
            attempt_count=row.attempt_count,
            last_error_code=row.last_error_code,
        )

    @staticmethod
    def _ensure_immutable_draft(
        current: CalendarOperationDraft, incoming: CalendarOperationDraft
    ) -> None:
        header = (
            "id",
            "user_id",
            "client_request_id",
            "request_fingerprint",
            "provider",
            "calendar_id",
            "root_plan_id",
            "revision",
            "plan_version",
            "created_at",
        )
        if any(getattr(current, field) != getattr(incoming, field) for field in header):
            raise RepositoryUniqueError("calendar_operation.immutable", incoming.id)
        if len(current.items) != len(incoming.items):
            raise RepositoryUniqueError("calendar_operation.items", incoming.id)
        for current_item, incoming_item in zip(
            current.items, incoming.items, strict=True
        ):
            fields = (
                "id",
                "operation_type",
                "session_id",
                "operation_key",
                "payload",
                "binding_id",
            )
            if any(
                getattr(current_item, field) != getattr(incoming_item, field)
                for field in fields
            ):
                raise RepositoryUniqueError(
                    "calendar_operation.item_immutable", incoming_item.id
                )

    @classmethod
    def _binding_row(cls, value: CalendarEventBinding) -> CalendarEventBindingModel:
        return CalendarEventBindingModel(
            id=str(value.id),
            user_id=str(value.user_id),
            provider=value.provider,
            calendar_id=value.calendar_id,
            root_plan_id=str(value.root_plan_id),
            session_id=str(value.session_id),
            external_event_id=value.external_event_id,
            stable_uid=value.stable_uid,
            last_payload_fingerprint=value.last_payload_fingerprint,
            status=value.status.value,
            created_at=cls._db_time(value.created_at),
            updated_at=cls._db_time(value.updated_at),
            version=value.version,
        )

    @classmethod
    def _binding_from_row(cls, row: CalendarEventBindingModel) -> CalendarEventBinding:
        return CalendarEventBinding(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            provider=row.provider,
            calendar_id=row.calendar_id,
            root_plan_id=UUID(row.root_plan_id),
            session_id=UUID(row.session_id),
            external_event_id=row.external_event_id,
            stable_uid=row.stable_uid,
            last_payload_fingerprint=row.last_payload_fingerprint,
            status=CalendarBindingStatus(row.status),
            created_at=cls._utc(row.created_at),
            updated_at=cls._utc(row.updated_at),
            version=row.version,
        )

    @classmethod
    def _apply_binding(
        cls, row: CalendarEventBindingModel, value: CalendarEventBinding
    ) -> None:
        replacement = cls._binding_row(value)
        for field in (
            "last_payload_fingerprint",
            "status",
            "updated_at",
            "version",
        ):
            setattr(row, field, getattr(replacement, field))

    @staticmethod
    def _binding_key(
        value: CalendarEventBinding,
    ) -> tuple[UUID, str, str, UUID, UUID]:
        return (
            value.user_id,
            value.provider,
            value.calendar_id,
            value.root_plan_id,
            value.session_id,
        )

    @classmethod
    def _attempt_row(
        cls, value: CalendarOperationAttempt
    ) -> CalendarOperationAttemptModel:
        return CalendarOperationAttemptModel(
            id=str(value.id),
            user_id=str(value.user_id),
            draft_id=str(value.draft_id),
            item_id=str(value.item_id),
            attempt_no=value.attempt_no,
            outcome=value.outcome.value,
            error_code=value.error_code,
            response_reference_hash=value.response_reference_hash,
            started_at=cls._db_time(value.started_at),
            finished_at=cls._db_time(value.finished_at),
        )

    @classmethod
    def _attempt_from_row(
        cls, row: CalendarOperationAttemptModel
    ) -> CalendarOperationAttempt:
        return CalendarOperationAttempt(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            draft_id=UUID(row.draft_id),
            item_id=UUID(row.item_id),
            attempt_no=row.attempt_no,
            outcome=CalendarAttemptOutcome(row.outcome),
            error_code=row.error_code,
            response_reference_hash=row.response_reference_hash,
            started_at=cls._utc(row.started_at),
            finished_at=cls._utc(row.finished_at),
        )

    @staticmethod
    def _is_retryable_lock_error(error: OperationalError) -> bool:
        arguments = getattr(error.orig, "args", ())
        return bool(arguments and arguments[0] in {1205, 1213})

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)

    @staticmethod
    def _optional_db_time(value: datetime | None) -> datetime | None:
        return (
            None if value is None else MySQLCalendarOperationRepository._db_time(value)
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )
