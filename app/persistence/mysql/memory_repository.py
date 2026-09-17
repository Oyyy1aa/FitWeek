"""MySQL implementation of the existing MemoryRepository aggregate protocol."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.context.models import ContextBuildAudit
from app.domain.memory.enums import (
    MemoryCandidateStatus,
    MemoryEvidenceType,
    MemorySource,
    MemoryStatus,
    MemoryType,
)
from app.domain.memory.models import (
    CandidateReviewOutcome,
    CandidateWriteOutcome,
    MemoryCandidate,
    MemoryEvidence,
    MemoryQueryResult,
    MemoryRecord,
    MemoryReplaceOutcome,
    MemoryWriteOutcome,
    UserMemory,
)
from app.persistence.mysql.models import (
    AuditEventModel,
    IdempotencyRecordModel,
    MemoryCandidateModel,
    MemoryEvidenceModel,
    MemoryItemModel,
    UserAccountModel,
)


class MySQLMemoryRepository:
    """Durable Memory aggregate adapter; all returned values are Domain objects."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_memory(
        self,
        *,
        client_request_id: str,
        payload_fingerprint: str,
        memory: UserMemory,
        evidence: MemoryEvidence,
    ) -> MemoryWriteOutcome:
        from app.domain.memory.errors import MemoryIdempotencyConflictError

        async with self._sessions() as session:
            async with session.begin():
                existing_request = await session.scalar(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.user_id == str(memory.user_id),
                        IdempotencyRecordModel.operation == "memory.create",
                        IdempotencyRecordModel.request_key == client_request_id,
                    )
                )
                if existing_request is not None:
                    if existing_request.payload_fingerprint != payload_fingerprint:
                        raise MemoryIdempotencyConflictError(
                            "Memory idempotency key was reused with another payload."
                        )
                    existing = await session.get(
                        MemoryItemModel, existing_request.resource_id
                    )
                    if existing is not None:
                        return MemoryWriteOutcome(
                            record=await self._record(session, existing),
                            created=False,
                        )
                row = await session.get(MemoryItemModel, str(memory.id))
                if row is not None:
                    return MemoryWriteOutcome(
                        record=await self._record(session, row), created=False
                    )
                session.add(self._memory_row(memory))
                await session.flush()
                session.add(self._evidence_row(evidence))
                session.add(
                    self._idempotency_row(
                        user_id=memory.user_id,
                        operation="memory.create",
                        request_key=client_request_id,
                        payload_fingerprint=payload_fingerprint,
                        resource_id=memory.id,
                        created_at=memory.created_at,
                    )
                )
            return MemoryWriteOutcome(
                record=MemoryRecord(memory=memory, evidence=(evidence,)), created=True
            )

    async def get_memory(
        self, user_id: UUID, memory_id: UUID, now: datetime
    ) -> MemoryRecord | None:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(MemoryItemModel)
                    .where(
                        MemoryItemModel.id == str(memory_id),
                        MemoryItemModel.user_id == str(user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                self._expire_memory_row(row, now)
                return await self._record(session, row)

    async def list_memories(self, user_id: UUID, now: datetime) -> list[MemoryRecord]:
        async with self._sessions() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(MemoryItemModel)
                        .where(MemoryItemModel.user_id == str(user_id))
                        .order_by(MemoryItemModel.created_at, MemoryItemModel.id)
                        .with_for_update()
                    )
                ).all()
                for row in rows:
                    self._expire_memory_row(row, now)
                return [await self._record(session, row) for row in rows]

    async def list_active_for_context(
        self, user_id: UUID, now: datetime
    ) -> MemoryQueryResult:
        records = await self.list_memories(user_id, now)
        active = tuple(
            record.memory
            for record in records
            if record.memory.status is MemoryStatus.ACTIVE
            and (record.memory.valid_until is None or record.memory.valid_until > now)
        )

        return MemoryQueryResult(
            memories=active,
            expired_filtered=0,
            deleted_filtered=sum(
                record.memory.status is MemoryStatus.DELETED for record in records
            ),
            pending_filtered=0,
            filtered_reasons={
                record.memory.id: record.memory.status.value
                for record in records
                if record.memory.status is not MemoryStatus.ACTIVE
            },
        )

    async def update_memory(
        self,
        *,
        user_id: UUID,
        memory: UserMemory,
        expected_version: int,
        evidence: MemoryEvidence,
    ) -> MemoryRecord | None:
        """Persist one user-scoped optimistic update and its audit evidence."""

        from app.domain.memory.errors import (
            MemoryNotActiveError,
            MemoryVersionConflictError,
        )

        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(MemoryItemModel)
                    .where(
                        MemoryItemModel.id == str(memory.id),
                        MemoryItemModel.user_id == str(user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                if row.version != expected_version:
                    raise MemoryVersionConflictError("Memory version does not match.")
                if row.status != MemoryStatus.ACTIVE.value:
                    raise MemoryNotActiveError("Only ACTIVE Memory can be updated.")
                if memory.version != expected_version + 1:
                    raise MemoryVersionConflictError(
                        "Memory update must advance exactly one version."
                    )
                row.memory_type = memory.memory_type.value
                row.status = memory.status.value
                row.content = memory.display_value
                row.normalized_content = memory.normalized_value
                row.scope = memory.key
                row.memory_key = memory.key
                row.source = memory.source.value
                row.confidence = memory.confidence
                row.valid_from = self._db_time(memory.valid_from)
                row.confirmed_at = self._db_time_or_none(memory.confirmed_at)
                row.deleted_at = self._db_time_or_none(memory.deleted_at)
                row.expires_at = self._db_time_or_none(memory.valid_until)
                row.updated_at = self._db_time(memory.updated_at)
                row.version = memory.version
                session.add(self._evidence_row(evidence))
                await session.flush()
                return await self._record(session, row)

    async def delete_memory(
        self,
        *,
        user_id: UUID,
        memory_id: UUID,
        expected_version: int,
        now: datetime,
    ) -> MemoryRecord | None:
        from app.domain.memory.errors import (
            MemoryNotActiveError,
            MemoryVersionConflictError,
        )

        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(MemoryItemModel)
                    .where(
                        MemoryItemModel.id == str(memory_id),
                        MemoryItemModel.user_id == str(user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                current = await self._record(session, row)
                if current.memory.status is MemoryStatus.DELETED:
                    return current
                if row.version != expected_version:
                    raise MemoryVersionConflictError("Memory version does not match.")
                if current.memory.status is not MemoryStatus.ACTIVE:
                    raise MemoryNotActiveError("Only ACTIVE Memory can be deleted.")
                deleted = current.memory.delete(now)
                row.status = deleted.status.value
                row.updated_at = self._db_time(deleted.updated_at)
                row.deleted_at = self._db_time_or_none(deleted.deleted_at)
                row.version = deleted.version
                await session.flush()
                return await self._record(session, row)

    async def replace_memory(
        self,
        *,
        user_id: UUID,
        memory_id: UUID,
        expected_version: int,
        client_request_id: str,
        payload_fingerprint: str,
        replacement: UserMemory,
        evidence: MemoryEvidence,
        now: datetime,
    ) -> MemoryReplaceOutcome | None:
        from app.domain.memory.errors import (
            MemoryNotActiveError,
            MemoryVersionConflictError,
        )

        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(MemoryItemModel)
                    .where(
                        MemoryItemModel.id == str(memory_id),
                        MemoryItemModel.user_id == str(user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                previous = await self._record(session, row)
                if row.version != expected_version:
                    raise MemoryVersionConflictError("Memory version does not match.")
                if previous.memory.status is not MemoryStatus.ACTIVE:
                    raise MemoryNotActiveError("Only ACTIVE Memory can be replaced.")
                existing_request = await session.scalar(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.user_id == str(user_id),
                        IdempotencyRecordModel.operation == "memory.replace",
                        IdempotencyRecordModel.request_key == client_request_id,
                    )
                )
                if existing_request is not None:
                    from app.domain.memory.errors import MemoryIdempotencyConflictError

                    if existing_request.payload_fingerprint != payload_fingerprint:
                        raise MemoryIdempotencyConflictError(
                            "Memory replacement key was reused with another payload."
                        )
                    replacement_row = await session.get(
                        MemoryItemModel, existing_request.resource_id
                    )
                    if replacement_row is not None:
                        return MemoryReplaceOutcome(
                            previous=previous,
                            replacement=await self._record(session, replacement_row),
                            created=False,
                        )
                superseded = previous.memory.supersede(now)
                row.status = superseded.status.value
                row.updated_at = self._db_time(superseded.updated_at)
                row.version = superseded.version
                session.add(self._memory_row(replacement))
                await session.flush()
                session.add(self._evidence_row(evidence))
                session.add(
                    self._idempotency_row(
                        user_id=user_id,
                        operation="memory.replace",
                        request_key=client_request_id,
                        payload_fingerprint=payload_fingerprint,
                        resource_id=replacement.id,
                        created_at=now,
                    )
                )
                replacement_row = await session.get(
                    MemoryItemModel, str(replacement.id)
                )
                if replacement_row is None:
                    raise MemoryVersionConflictError(
                        "Replacement Memory was not stored."
                    )
                await session.flush()
                return MemoryReplaceOutcome(
                    previous=await self._record(session, row),
                    replacement=await self._record(session, replacement_row),
                    created=True,
                )

    async def create_candidate(
        self,
        *,
        client_request_id: str,
        payload_fingerprint: str,
        candidate: MemoryCandidate,
    ) -> CandidateWriteOutcome:
        from app.domain.memory.errors import MemoryCandidateIdempotencyConflictError

        async with self._sessions() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.user_id == str(candidate.user_id),
                        IdempotencyRecordModel.operation == "memory.candidate.create",
                        IdempotencyRecordModel.request_key == client_request_id,
                    )
                )
                if existing is not None:
                    if existing.payload_fingerprint != payload_fingerprint:
                        raise MemoryCandidateIdempotencyConflictError(
                            "Candidate idempotency key was reused with another payload."
                        )
                    row = await session.get(MemoryCandidateModel, existing.resource_id)
                    if row is None:
                        raise MemoryCandidateIdempotencyConflictError(
                            "Candidate idempotency record has no Candidate."
                        )
                    return CandidateWriteOutcome(
                        candidate=self._candidate(row), created=False
                    )
                candidate_row = self._candidate_row(candidate)
                candidate_row.idempotency_key = client_request_id
                candidate_row.payload_fingerprint = payload_fingerprint
                session.add(candidate_row)
                session.add(
                    self._idempotency_row(
                        user_id=candidate.user_id,
                        operation="memory.candidate.create",
                        request_key=client_request_id,
                        payload_fingerprint=payload_fingerprint,
                        resource_id=candidate.id,
                        created_at=candidate.created_at,
                    )
                )
            return CandidateWriteOutcome(candidate=candidate, created=True)

    async def get_candidate(
        self, user_id: UUID, candidate_id: UUID, now: datetime
    ) -> MemoryCandidate | None:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(MemoryCandidateModel)
                    .where(
                        MemoryCandidateModel.id == str(candidate_id),
                        MemoryCandidateModel.user_id == str(user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                self._expire_candidate_row(row, now)
                return self._candidate(row)

    async def list_candidates(
        self, user_id: UUID, now: datetime
    ) -> list[MemoryCandidate]:
        async with self._sessions() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(MemoryCandidateModel)
                        .where(MemoryCandidateModel.user_id == str(user_id))
                        .order_by(
                            MemoryCandidateModel.created_at, MemoryCandidateModel.id
                        )
                        .with_for_update()
                    )
                ).all()
                for row in rows:
                    self._expire_candidate_row(row, now)
                return [self._candidate(row) for row in rows]

    async def accept_candidate(
        self,
        *,
        user_id: UUID,
        candidate_id: UUID,
        expected_version: int,
        client_request_id: str,
        payload_fingerprint: str,
        confirmed_value: str,
        memory: UserMemory,
        evidence: MemoryEvidence,
        now: datetime,
    ) -> CandidateReviewOutcome:
        from app.domain.memory.errors import (
            MemoryCandidateAlreadyAcceptedError,
            MemoryCandidateExpiredError,
            MemoryCandidateIdempotencyConflictError,
            MemoryCandidateNotFoundError,
            MemoryCandidateRejectedError,
            MemoryCandidateVersionConflictError,
        )

        async with self._sessions() as session:
            async with session.begin():
                request = await session.scalar(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.user_id == str(user_id),
                        IdempotencyRecordModel.operation == "memory.candidate.accept",
                        IdempotencyRecordModel.request_key == client_request_id,
                    )
                )
                if request is not None:
                    if request.payload_fingerprint != payload_fingerprint:
                        raise MemoryCandidateIdempotencyConflictError(
                            "Candidate acceptance key was reused with another payload."
                        )
                    accepted_row = await session.get(
                        MemoryCandidateModel, request.resource_id
                    )
                    if accepted_row is None:
                        raise MemoryCandidateIdempotencyConflictError(
                            "Candidate acceptance record has no Candidate."
                        )
                    accepted = self._candidate(accepted_row)
                    accepted_memory = await session.get(MemoryItemModel, str(memory.id))
                    return CandidateReviewOutcome(
                        candidate=accepted,
                        memory=None
                        if accepted_memory is None
                        else await self._record(session, accepted_memory),
                        created=False,
                    )
                row = await session.scalar(
                    select(MemoryCandidateModel)
                    .where(
                        MemoryCandidateModel.id == str(candidate_id),
                        MemoryCandidateModel.user_id == str(user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    raise MemoryCandidateNotFoundError(
                        "Memory Candidate was not found."
                    )
                candidate = self._candidate(row)
                if (
                    candidate.status is MemoryCandidateStatus.EXPIRED
                    or candidate.expires_at <= now
                ):
                    raise MemoryCandidateExpiredError("Memory Candidate has expired.")
                if candidate.status is MemoryCandidateStatus.REJECTED:
                    raise MemoryCandidateRejectedError(
                        "Rejected Candidate cannot be accepted."
                    )
                if candidate.status is MemoryCandidateStatus.ACCEPTED:
                    raise MemoryCandidateAlreadyAcceptedError(
                        "Candidate is already accepted."
                    )
                if candidate.version != expected_version:
                    raise MemoryCandidateVersionConflictError(
                        "Memory Candidate version does not match."
                    )
                if candidate.proposed_value != confirmed_value:
                    from app.domain.memory.errors import MemoryConflictError

                    raise MemoryConflictError("Confirmed value must match Candidate.")
                conflicts = (
                    await session.scalars(
                        select(MemoryItemModel)
                        .where(
                            MemoryItemModel.user_id == str(user_id),
                            MemoryItemModel.memory_type == memory.memory_type.value,
                            MemoryItemModel.memory_key == memory.key,
                            MemoryItemModel.status == MemoryStatus.ACTIVE.value,
                        )
                        .with_for_update()
                    )
                ).all()
                for conflict in conflicts:
                    conflict.status = MemoryStatus.SUPERSEDED.value
                    conflict.updated_at = self._db_time(now)
                    conflict.version += 1
                session.add(self._memory_row(memory))
                await session.flush()
                session.add(self._evidence_row(evidence))
                accepted = candidate.accept(now)
                row.status = accepted.status.value
                row.reviewed_at = self._db_time(now)
                row.version = accepted.version
                session.add(
                    self._idempotency_row(
                        user_id=user_id,
                        operation="memory.candidate.accept",
                        request_key=client_request_id,
                        payload_fingerprint=payload_fingerprint,
                        resource_id=candidate_id,
                        created_at=now,
                    )
                )
            return CandidateReviewOutcome(
                candidate=accepted,
                memory=MemoryRecord(memory=memory, evidence=(evidence,)),
                created=True,
            )

    async def reject_candidate(
        self,
        *,
        user_id: UUID,
        candidate_id: UUID,
        expected_version: int,
        client_request_id: str,
        payload_fingerprint: str,
        now: datetime,
    ) -> CandidateReviewOutcome:
        from app.domain.memory.errors import (
            MemoryCandidateAlreadyAcceptedError,
            MemoryCandidateExpiredError,
            MemoryCandidateIdempotencyConflictError,
            MemoryCandidateNotFoundError,
            MemoryCandidateRejectedError,
            MemoryCandidateVersionConflictError,
        )

        async with self._sessions() as session:
            async with session.begin():
                request = await session.scalar(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.user_id == str(user_id),
                        IdempotencyRecordModel.operation == "memory.candidate.reject",
                        IdempotencyRecordModel.request_key == client_request_id,
                    )
                )
                if request is not None:
                    if request.payload_fingerprint != payload_fingerprint:
                        raise MemoryCandidateIdempotencyConflictError(
                            "Candidate rejection key was reused with another payload."
                        )
                    rejected_row = await session.get(
                        MemoryCandidateModel, request.resource_id
                    )
                    if rejected_row is None:
                        raise MemoryCandidateIdempotencyConflictError(
                            "Candidate rejection record has no Candidate."
                        )
                    return CandidateReviewOutcome(
                        candidate=self._candidate(rejected_row),
                        memory=None,
                        created=False,
                    )
                row = await session.scalar(
                    select(MemoryCandidateModel)
                    .where(
                        MemoryCandidateModel.id == str(candidate_id),
                        MemoryCandidateModel.user_id == str(user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    raise MemoryCandidateNotFoundError(
                        "Memory Candidate was not found."
                    )
                candidate = self._candidate(row)
                if (
                    candidate.status is MemoryCandidateStatus.EXPIRED
                    or candidate.expires_at <= now
                ):
                    raise MemoryCandidateExpiredError("Memory Candidate has expired.")
                if candidate.status is MemoryCandidateStatus.ACCEPTED:
                    raise MemoryCandidateAlreadyAcceptedError(
                        "Accepted Candidate cannot be rejected."
                    )
                if candidate.status is MemoryCandidateStatus.REJECTED:
                    raise MemoryCandidateRejectedError(
                        "Candidate was already rejected."
                    )
                if candidate.version != expected_version:
                    raise MemoryCandidateVersionConflictError(
                        "Memory Candidate version does not match."
                    )
                rejected = candidate.reject(now)
                row.status = rejected.status.value
                row.reviewed_at = self._db_time(now)
                row.version = rejected.version
                session.add(
                    self._idempotency_row(
                        user_id=user_id,
                        operation="memory.candidate.reject",
                        request_key=client_request_id,
                        payload_fingerprint=payload_fingerprint,
                        resource_id=candidate_id,
                        created_at=now,
                    )
                )
            return CandidateReviewOutcome(candidate=rejected, memory=None, created=True)

    async def save_context_audit(self, audit: ContextBuildAudit) -> ContextBuildAudit:
        async with self._sessions() as session:
            async with session.begin():
                existing = await session.get(AuditEventModel, str(audit.id))
                if existing is None:
                    session.add(
                        AuditEventModel(
                            id=str(audit.id),
                            user_id=str(audit.user_id),
                            run_id=None if audit.run_id is None else str(audit.run_id),
                            step_id=None
                            if audit.step_id is None
                            else str(audit.step_id),
                            sequence_no=None,
                            event_type="CONTEXT_BUILD_AUDIT",
                            event_metadata=self._audit_payload(audit),
                            occurred_at=self._db_time(audit.created_at),
                        )
                    )
            return audit

    async def get_context_audit(
        self, user_id: UUID, audit_id: UUID
    ) -> ContextBuildAudit | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(AuditEventModel).where(
                    AuditEventModel.id == str(audit_id),
                    AuditEventModel.user_id == str(user_id),
                    AuditEventModel.event_type == "CONTEXT_BUILD_AUDIT",
                )
            )
            return None if row is None else self._audit(row)

    async def reset(self) -> None:
        bind = self._sessions.kw.get("bind")
        database_name = str(getattr(getattr(bind, "url", None), "database", ""))
        if "fitweek_test" not in database_name:
            raise RuntimeError("Memory repository reset is restricted to fitweek_test.")
        async with self._sessions() as session, session.begin():
            test_users = select(UserAccountModel.id).where(
                UserAccountModel.email.like("memory-%@fitweek.test")
            )
            memory_ids = select(MemoryItemModel.id).where(
                MemoryItemModel.user_id.in_(test_users)
            )
            await session.execute(
                delete(MemoryEvidenceModel).where(
                    MemoryEvidenceModel.memory_id.in_(memory_ids)
                )
            )
            await session.execute(
                delete(AuditEventModel).where(AuditEventModel.user_id.in_(test_users))
            )
            await session.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.user_id.in_(test_users)
                )
            )
            await session.execute(
                delete(MemoryCandidateModel).where(
                    MemoryCandidateModel.user_id.in_(test_users)
                )
            )
            await session.execute(
                delete(MemoryItemModel).where(MemoryItemModel.user_id.in_(test_users))
            )

    @staticmethod
    def _memory_row(value: UserMemory) -> MemoryItemModel:
        return MemoryItemModel(
            id=str(value.id),
            user_id=str(value.user_id),
            memory_type=value.memory_type.value,
            status=value.status.value,
            content=value.display_value,
            normalized_content=value.normalized_value,
            scope=value.key,
            memory_key=value.key,
            source=value.source.value,
            confidence=value.confidence,
            valid_from=MySQLMemoryRepository._db_time(value.valid_from),
            confirmed_at=MySQLMemoryRepository._db_time_or_none(value.confirmed_at),
            deleted_at=MySQLMemoryRepository._db_time_or_none(value.deleted_at),
            expires_at=None
            if value.valid_until is None
            else value.valid_until.astimezone(UTC).replace(tzinfo=None),
            created_at=value.created_at.astimezone(UTC).replace(tzinfo=None),
            updated_at=value.updated_at.astimezone(UTC).replace(tzinfo=None),
            version=value.version,
        )

    @staticmethod
    def _evidence_row(value: MemoryEvidence) -> MemoryEvidenceModel:
        return MemoryEvidenceModel(
            id=str(value.id),
            memory_id=str(value.memory_id),
            evidence_type=value.evidence_type.value,
            reference=value.source_reference,
            payload={
                "summary": value.evidence_summary,
                "source_occurred_at": value.source_occurred_at.isoformat(),
                "fingerprint": value.content_fingerprint,
            },
            created_at=value.created_at.astimezone(UTC).replace(tzinfo=None),
        )

    async def _record(
        self, session: AsyncSession, row: MemoryItemModel
    ) -> MemoryRecord:
        evidence_rows = (
            await session.scalars(
                select(MemoryEvidenceModel)
                .where(MemoryEvidenceModel.memory_id == row.id)
                .order_by(MemoryEvidenceModel.created_at)
            )
        ).all()
        memory = UserMemory(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            memory_type=MemoryType(row.memory_type),
            key=row.memory_key or row.scope,
            normalized_value=row.normalized_content,
            display_value=row.content,
            status=MemoryStatus(row.status),
            source=MemorySource(row.source or MemorySource.USER_EXPLICIT.value),
            confidence=None if row.confidence is None else Decimal(row.confidence),
            valid_from=self._utc(row.valid_from or row.created_at),
            valid_until=None if row.expires_at is None else self._utc(row.expires_at),
            confirmed_at=None
            if row.confirmed_at is None
            else self._utc(row.confirmed_at),
            created_at=self._utc(row.created_at),
            updated_at=self._utc(row.updated_at),
            deleted_at=None if row.deleted_at is None else self._utc(row.deleted_at),
            version=row.version,
        )
        evidence = tuple(
            MemoryEvidence(
                id=UUID(item.id),
                memory_id=memory.id,
                evidence_type=MemoryEvidenceType(item.evidence_type),
                source_reference=item.reference,
                evidence_summary=str(item.payload["summary"]),
                source_occurred_at=datetime.fromisoformat(
                    str(item.payload["source_occurred_at"])
                ),
                created_at=self._utc(item.created_at),
                content_fingerprint=str(item.payload["fingerprint"]),
            )
            for item in evidence_rows
        )
        return MemoryRecord(memory=memory, evidence=evidence)

    @staticmethod
    def _candidate_row(value: MemoryCandidate) -> MemoryCandidateModel:
        return MemoryCandidateModel(
            id=str(value.id),
            user_id=str(value.user_id),
            memory_type=value.memory_type.value,
            status=value.status.value,
            proposed_content=value.proposed_value,
            source=value.source.value,
            proposed_key=value.proposed_key,
            source_reference=value.source_reference,
            evidence_summary=value.evidence_summary,
            confidence=value.confidence,
            expires_at=MySQLMemoryRepository._db_time(value.expires_at),
            created_at=MySQLMemoryRepository._db_time(value.created_at),
            reviewed_at=MySQLMemoryRepository._db_time_or_none(value.reviewed_at),
            version=value.version,
        )

    @staticmethod
    def _candidate(row: MemoryCandidateModel) -> MemoryCandidate:
        return MemoryCandidate(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            memory_type=MemoryType(row.memory_type),
            proposed_key=row.proposed_key or "legacy",
            proposed_value=row.proposed_content,
            source=MemorySource(row.source),
            source_reference=row.source_reference or "legacy:unknown",
            evidence_summary=row.evidence_summary or "Legacy Memory Candidate.",
            confidence=None if row.confidence is None else Decimal(row.confidence),
            status=MemoryCandidateStatus(row.status),
            created_at=MySQLMemoryRepository._utc(row.created_at),
            expires_at=MySQLMemoryRepository._utc(row.expires_at or row.created_at),
            reviewed_at=MySQLMemoryRepository._utc(row.reviewed_at)
            if row.reviewed_at is not None
            else None,
            version=row.version,
        )

    @staticmethod
    def _expire_memory_row(row: MemoryItemModel, now: datetime) -> None:
        if (
            row.status == MemoryStatus.ACTIVE.value
            and row.expires_at is not None
            and MySQLMemoryRepository._utc(row.expires_at) <= now
        ):
            row.status = MemoryStatus.EXPIRED.value
            row.updated_at = MySQLMemoryRepository._db_time(now)
            row.version += 1

    @staticmethod
    def _expire_candidate_row(row: MemoryCandidateModel, now: datetime) -> None:
        if (
            row.status == MemoryCandidateStatus.PENDING_REVIEW.value
            and row.expires_at is not None
            and MySQLMemoryRepository._utc(row.expires_at) <= now
        ):
            row.status = MemoryCandidateStatus.EXPIRED.value
            row.version += 1

    @staticmethod
    def _idempotency_row(
        *,
        user_id: UUID,
        operation: str,
        request_key: str,
        payload_fingerprint: str,
        resource_id: UUID | None,
        created_at: datetime,
    ) -> IdempotencyRecordModel:
        return IdempotencyRecordModel(
            id=str(uuid4()),
            user_id=str(user_id),
            operation=operation,
            request_key=request_key,
            payload_fingerprint=payload_fingerprint,
            resource_id=None if resource_id is None else str(resource_id),
            created_at=MySQLMemoryRepository._db_time(created_at),
        )

    @staticmethod
    def _audit_payload(audit: ContextBuildAudit) -> dict[str, object]:
        return {
            "agent_type": audit.agent_type.value,
            "contract_version": audit.context_contract_version,
            "request_fingerprint": audit.request_fingerprint,
            "included_memory_ids": [str(item) for item in audit.included_memory_ids],
            "excluded_memory_ids": [str(item) for item in audit.excluded_memory_ids],
            "exclusion_reasons": {
                str(key): value for key, value in audit.exclusion_reasons.items()
            },
            "conflicts": [
                {
                    "higher": item.higher_priority_source,
                    "lower": item.lower_priority_source,
                    "key": item.key,
                    "resolution": item.resolution,
                }
                for item in audit.conflicts
            ],
            "budget_before": audit.budget_before,
            "budget_after": audit.budget_after,
            "degraded_mode": audit.degraded_mode.value,
            "profile_draft_id": None
            if audit.profile_draft_id is None
            else str(audit.profile_draft_id),
            "plan_id": None if audit.plan_id is None else str(audit.plan_id),
        }

    @staticmethod
    def _audit(row: AuditEventModel) -> ContextBuildAudit:
        from app.domain.context.enums import AgentType, ContextDegradedMode
        from app.domain.context.models import ContextConflict

        payload = row.event_metadata
        conflicts = tuple(
            ContextConflict(
                higher_priority_source=str(item["higher"]),
                lower_priority_source=str(item["lower"]),
                key=str(item["key"]),
                resolution=str(item["resolution"]),
            )
            for item in payload["conflicts"]
        )
        return ContextBuildAudit(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            agent_type=AgentType(str(payload["agent_type"])),
            context_contract_version=str(payload["contract_version"]),
            request_fingerprint=str(payload["request_fingerprint"]),
            included_memory_ids=tuple(
                UUID(str(item)) for item in payload["included_memory_ids"]
            ),
            excluded_memory_ids=tuple(
                UUID(str(item)) for item in payload["excluded_memory_ids"]
            ),
            exclusion_reasons={
                UUID(key): str(value)
                for key, value in dict(payload["exclusion_reasons"]).items()
            },
            conflicts=conflicts,
            budget_before=int(payload["budget_before"]),
            budget_after=int(payload["budget_after"]),
            degraded_mode=ContextDegradedMode(str(payload["degraded_mode"])),
            created_at=MySQLMemoryRepository._utc(row.occurred_at),
            run_id=None if row.run_id is None else UUID(row.run_id),
            step_id=None if row.step_id is None else UUID(row.step_id),
            profile_draft_id=None
            if payload["profile_draft_id"] is None
            else UUID(str(payload["profile_draft_id"])),
            plan_id=None
            if payload["plan_id"] is None
            else UUID(str(payload["plan_id"])),
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)

    @staticmethod
    def _db_time_or_none(value: datetime | None) -> datetime | None:
        return None if value is None else MySQLMemoryRepository._db_time(value)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )
