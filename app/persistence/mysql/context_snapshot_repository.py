"""Immutable, user-scoped Context Snapshot persistence for MySQL."""

from datetime import UTC, datetime
from math import ceil
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.context.enums import AgentType, ContextDegradedMode, ContextSectionName
from app.domain.context.models import (
    BuiltContext,
    ContextConflict,
    ContextItem,
    ContextSection,
    ContextSnapshotReference,
    EntityVersionReference,
    FrozenContextSnapshot,
)
from app.domain.memory.errors import ContextSnapshotVersionMismatchError
from app.persistence.mysql.models import ContextSnapshotModel


class MySQLContextSnapshotRepository:
    """Persist a frozen context once per user/agent/scope without mutation."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(
        self, user_id: UUID, snapshot_id: UUID
    ) -> FrozenContextSnapshot | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ContextSnapshotModel).where(
                    ContextSnapshotModel.id == str(snapshot_id),
                    ContextSnapshotModel.user_id == str(user_id),
                )
            )
            return None if row is None else self._to_domain(row)

    async def get_by_scope(
        self, user_id: UUID, agent_type: AgentType, scope_id: str
    ) -> FrozenContextSnapshot | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ContextSnapshotModel).where(
                    ContextSnapshotModel.user_id == str(user_id),
                    ContextSnapshotModel.agent_type == agent_type.value,
                    ContextSnapshotModel.scope_id == scope_id,
                )
            )
            return None if row is None else self._to_domain(row)

    async def save(
        self, *, scope_id: str, snapshot: FrozenContextSnapshot
    ) -> FrozenContextSnapshot:
        reference = snapshot.reference
        try:
            async with self._sessions() as session:
                async with session.begin():
                    existing = await session.scalar(
                        select(ContextSnapshotModel).where(
                            ContextSnapshotModel.user_id == str(reference.user_id),
                            ContextSnapshotModel.agent_type
                            == reference.agent_type.value,
                            ContextSnapshotModel.scope_id == scope_id,
                        )
                    )
                    if existing is not None:
                        if existing.fingerprint != reference.context_fingerprint:
                            raise ContextSnapshotVersionMismatchError(
                                "The Context Snapshot scope is already frozen."
                            )
                        return self._to_domain(existing)
                    session.add(
                        ContextSnapshotModel(
                            id=str(reference.id),
                            user_id=str(reference.user_id),
                            agent_type=reference.agent_type.value,
                            scope_id=scope_id,
                            fingerprint=reference.context_fingerprint,
                            contract_version=reference.contract_version,
                            policy_version=reference.policy_version,
                            content=self._payload(snapshot),
                            character_count=snapshot.context.character_count,
                            token_estimate=max(
                                1, ceil(snapshot.context.character_count / 4)
                            ),
                            created_at=self._db_time(reference.created_at),
                        )
                    )
            return snapshot
        except IntegrityError as exc:
            winner = await self.get_by_scope(
                reference.user_id,
                reference.agent_type,
                scope_id,
            )
            if winner is None:
                raise
            if winner.reference.context_fingerprint != reference.context_fingerprint:
                raise ContextSnapshotVersionMismatchError(
                    "The Context Snapshot scope is already frozen."
                ) from exc
            return winner

    async def reset(self) -> None:
        """Test-only reset is deliberately unsupported for shared SQL storage."""

        raise RuntimeError("MySQL Context Snapshot storage cannot be globally reset.")

    @classmethod
    def _payload(cls, snapshot: FrozenContextSnapshot) -> dict[str, object]:
        reference = snapshot.reference
        context = snapshot.context
        return {
            "reference": {
                "context_audit_id": str(reference.context_audit_id),
                "profile_id": None
                if reference.profile_id is None
                else str(reference.profile_id),
                "profile_version": reference.profile_version,
                "constraint_versions": cls._versions(reference.constraint_versions),
                "memory_versions": cls._versions(reference.memory_versions),
                "degraded_mode": reference.degraded_mode.value,
                "created_at": reference.created_at.isoformat(),
            },
            "context": {
                "id": str(context.id),
                "audit_id": str(context.audit_id),
                "sections": [
                    {
                        "name": section.name.value,
                        "source": section.source,
                        "generated_at": section.generated_at.isoformat(),
                        "version": section.version,
                        "items": [
                            {
                                "key": item.key,
                                "value": item.value,
                                "source": item.source,
                                "source_reference": item.source_reference,
                            }
                            for item in section.items
                        ],
                    }
                    for section in context.sections
                ],
                "conflicts": [
                    {
                        "higher_priority_source": item.higher_priority_source,
                        "lower_priority_source": item.lower_priority_source,
                        "key": item.key,
                        "resolution": item.resolution,
                    }
                    for item in context.conflicts
                ],
            },
        }

    @staticmethod
    def _versions(
        values: tuple[EntityVersionReference, ...],
    ) -> list[dict[str, object]]:
        return [{"id": str(item.id), "version": item.version} for item in values]

    @classmethod
    def _to_domain(cls, row: ContextSnapshotModel) -> FrozenContextSnapshot:
        payload = row.content
        reference_data = cls._as_dict(payload["reference"])
        context_data = cls._as_dict(payload["context"])
        # MySQL installations created by the historical 0002 migration can use
        # second precision.  The immutable reference retains its exact UTC value
        # in the already-safe snapshot payload, so restart reads do not alter the
        # frozen identity or its fingerprint inputs.
        stored_created_at = reference_data.get("created_at")
        created_at = (
            cls._utc(row.created_at)
            if stored_created_at is None
            else cls._parse_time(str(stored_created_at))
        )
        reference = ContextSnapshotReference(
            id=UUID(row.id),
            user_id=UUID(row.user_id),
            agent_type=AgentType(row.agent_type),
            contract_version=row.contract_version,
            policy_version=row.policy_version,
            context_fingerprint=row.fingerprint,
            context_audit_id=UUID(str(reference_data["context_audit_id"])),
            profile_id=(
                None
                if reference_data["profile_id"] is None
                else UUID(str(reference_data["profile_id"]))
            ),
            profile_version=(
                None
                if reference_data["profile_version"] is None
                else int(cast(str | int, reference_data["profile_version"]))
            ),
            constraint_versions=cls._version_refs(
                reference_data["constraint_versions"]
            ),
            memory_versions=cls._version_refs(reference_data["memory_versions"]),
            degraded_mode=ContextDegradedMode(str(reference_data["degraded_mode"])),
            created_at=created_at,
        )
        sections = tuple(
            ContextSection(
                name=ContextSectionName(str(section["name"])),
                source=str(section["source"]),
                generated_at=cls._parse_time(str(section["generated_at"])),
                version=str(section["version"]),
                items=tuple(
                    ContextItem(
                        key=str(item["key"]),
                        value=str(item["value"]),
                        source=str(item["source"]),
                        source_reference=(
                            None
                            if item["source_reference"] is None
                            else str(item["source_reference"])
                        ),
                    )
                    for item in cls._as_list(section["items"])
                    for item in (cls._as_dict(item),)
                ),
            )
            for section in cls._as_list(context_data["sections"])
            for section in (cls._as_dict(section),)
        )
        conflicts = tuple(
            ContextConflict(
                higher_priority_source=str(item["higher_priority_source"]),
                lower_priority_source=str(item["lower_priority_source"]),
                key=str(item["key"]),
                resolution=str(item["resolution"]),
            )
            for item in cls._as_list(context_data["conflicts"])
            for item in (cls._as_dict(item),)
        )
        return FrozenContextSnapshot(
            reference=reference,
            context=BuiltContext(
                id=UUID(str(context_data["id"])),
                user_id=reference.user_id,
                agent_type=reference.agent_type,
                contract_version=reference.contract_version,
                sections=sections,
                conflicts=conflicts,
                degraded_mode=reference.degraded_mode,
                character_count=row.character_count,
                audit_id=UUID(str(context_data["audit_id"])),
                created_at=created_at,
            ),
        )

    @classmethod
    def _version_refs(cls, value: object) -> tuple[EntityVersionReference, ...]:
        return tuple(
            EntityVersionReference(
                id=UUID(str(item["id"])),
                version=int(cast(str | int, item["version"])),
            )
            for raw in cls._as_list(value)
            for item in (cls._as_dict(raw),)
        )

    @staticmethod
    def _as_dict(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ContextSnapshotVersionMismatchError(
                "Stored Context Snapshot is invalid."
            )
        return value

    @staticmethod
    def _as_list(value: object) -> list[object]:
        if not isinstance(value, list):
            raise ContextSnapshotVersionMismatchError(
                "Stored Context Snapshot is invalid."
            )
        return value

    @staticmethod
    def _parse_time(value: str) -> datetime:
        return MySQLContextSnapshotRepository._utc(datetime.fromisoformat(value))

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)
