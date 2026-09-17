"""Create and reuse privacy-preserving, process-local Context snapshots."""

import json
from uuid import NAMESPACE_URL, UUID, uuid5

from app.context.builder import DeterministicContextBuilder
from app.context.registry import ContextContractRegistry
from app.domain.context.models import (
    BuiltContext,
    ContextBuildCommand,
    ContextSnapshotReference,
    EntityVersionReference,
    FrozenContextSnapshot,
)
from app.domain.context.repositories import ContextSnapshotRepository
from app.domain.memory.errors import (
    ContextSnapshotNotFoundError,
    ContextSnapshotVersionMismatchError,
)
from app.domain.memory.repositories import MemoryRepository
from app.domain.profiles.repositories import ProfileRepository
from app.memory.metrics import MemoryMetrics
from app.memory.normalization import fingerprint


class ContextSnapshotService:
    """Freeze one effective Context per explicit request/run-step scope."""

    def __init__(
        self,
        *,
        builder: DeterministicContextBuilder,
        registry: ContextContractRegistry,
        snapshots: ContextSnapshotRepository,
        profiles: ProfileRepository,
        memories: MemoryRepository,
        metrics: MemoryMetrics,
    ) -> None:
        self._builder = builder
        self._registry = registry
        self._snapshots = snapshots
        self._profiles = profiles
        self._memories = memories
        self._metrics = metrics

    async def build(
        self,
        *,
        user_id: UUID,
        command: ContextBuildCommand,
        scope_id: str,
    ) -> FrozenContextSnapshot:
        normalized_scope = scope_id.strip()
        if not normalized_scope:
            raise ValueError("Context snapshot scope_id must not be blank.")
        existing = await self._snapshots.get_by_scope(
            user_id,
            command.agent_type,
            normalized_scope,
        )
        if existing is not None:
            await self._validate_versions(existing)
            self._metrics.increment("context_snapshots_reused")
            return existing
        built = await self._builder.build(user_id, command)
        contract = self._registry.get(command.agent_type, built.contract_version)
        profile = await self._profiles.get_by_user_id(user_id)
        constraints = (
            tuple(await self._profiles.list_constraints(profile.id)) if profile else ()
        )
        memory_refs: list[EntityVersionReference] = []
        audit = await self._memories.get_context_audit(user_id, built.audit_id)
        if audit is None:
            self._metrics.increment("context_snapshot_failures")
            raise ContextSnapshotNotFoundError("Context Audit was not found.")
        for memory_id in audit.included_memory_ids:
            record = await self._memories.get_memory(
                user_id,
                memory_id,
                built.created_at,
            )
            if record is None:
                self._metrics.increment("context_snapshot_failures")
                raise ContextSnapshotVersionMismatchError(
                    "An included Memory changed before the Context was frozen."
                )
            memory_refs.append(
                EntityVersionReference(
                    id=record.memory.id,
                    version=record.memory.version,
                )
            )
        context_fingerprint = self._context_fingerprint(built, contract.policy_version)
        snapshot_id = uuid5(
            NAMESPACE_URL,
            (
                f"fitweek:context-snapshot:{user_id}:{command.agent_type.value}:"
                f"{normalized_scope}:{context_fingerprint}"
            ),
        )
        reference = ContextSnapshotReference(
            id=snapshot_id,
            user_id=user_id,
            agent_type=command.agent_type,
            contract_version=contract.version,
            policy_version=contract.policy_version,
            context_fingerprint=context_fingerprint,
            context_audit_id=built.audit_id,
            profile_id=profile.id if profile else None,
            profile_version=profile.version if profile else None,
            constraint_versions=tuple(
                sorted(
                    (
                        EntityVersionReference(id=item.id, version=item.version)
                        for item in constraints
                    ),
                    key=lambda item: str(item.id),
                )
            ),
            memory_versions=tuple(sorted(memory_refs, key=lambda item: str(item.id))),
            degraded_mode=built.degraded_mode,
            created_at=built.created_at,
        )
        saved = await self._snapshots.save(
            scope_id=normalized_scope,
            snapshot=FrozenContextSnapshot(reference=reference, context=built),
        )
        self._metrics.increment("context_snapshots_created")
        return saved

    async def get(
        self,
        *,
        user_id: UUID,
        snapshot_id: UUID,
        validate_versions: bool = True,
    ) -> FrozenContextSnapshot:
        snapshot = await self._snapshots.get(user_id, snapshot_id)
        if snapshot is None:
            self._metrics.increment("context_snapshot_failures")
            raise ContextSnapshotNotFoundError("Context Snapshot was not found.")
        if validate_versions:
            await self._validate_versions(snapshot)
        return snapshot

    async def _validate_versions(self, snapshot: FrozenContextSnapshot) -> None:
        reference = snapshot.reference
        profile = await self._profiles.get_by_user_id(reference.user_id)
        if reference.profile_id is not None and (
            profile is None
            or profile.id != reference.profile_id
            or profile.version != reference.profile_version
        ):
            self._metrics.increment("context_snapshot_failures")
            raise ContextSnapshotVersionMismatchError(
                "The Profile referenced by the Context Snapshot changed."
            )
        current_constraints = (
            await self._profiles.list_constraints(profile.id) if profile else []
        )
        by_constraint_id = {item.id: item.version for item in current_constraints}
        if any(
            by_constraint_id.get(item.id) != item.version
            for item in reference.constraint_versions
        ):
            self._metrics.increment("context_snapshot_failures")
            raise ContextSnapshotVersionMismatchError(
                "A Constraint referenced by the Context Snapshot changed."
            )
        for item in reference.memory_versions:
            record = await self._memories.get_memory(
                reference.user_id,
                item.id,
                snapshot.reference.created_at,
            )
            if record is None or record.memory.version != item.version:
                self._metrics.increment("context_snapshot_failures")
                raise ContextSnapshotVersionMismatchError(
                    "A Memory referenced by the Context Snapshot changed."
                )

    @staticmethod
    def _context_fingerprint(context: BuiltContext, policy_version: str) -> str:
        effective = {
            "agent_type": context.agent_type.value,
            "contract_version": context.contract_version,
            "policy_version": policy_version,
            "degraded_mode": context.degraded_mode.value,
            "sections": [
                {
                    "name": section.name.value,
                    "source": section.source,
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
                    "higher": item.higher_priority_source,
                    "lower": item.lower_priority_source,
                    "key": item.key,
                    "resolution": item.resolution,
                }
                for item in context.conflicts
            ],
        }
        # Only the digest is stored in the public reference.
        return fingerprint({"effective_context": json.dumps(effective, sort_keys=True)})
