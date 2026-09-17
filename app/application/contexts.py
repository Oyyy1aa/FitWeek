"""Current-user application boundary for Context build and Audit retrieval."""

from uuid import UUID, uuid4

from app.context.audit import ContextAuditService
from app.context.builder import DeterministicContextBuilder
from app.context.snapshot import ContextSnapshotService
from app.domain.context.models import (
    BuiltContext,
    ContextBuildAudit,
    ContextBuildCommand,
    ContextSnapshotReference,
    FrozenContextSnapshot,
)
from app.domain.users.models import UserAccount
from app.observability.context import (
    ObservabilityContext,
    current_observability_context,
)
from app.observability.facade import ObservabilityFacade


class ContextApplicationService:
    def __init__(
        self,
        builder: DeterministicContextBuilder,
        audits: ContextAuditService,
        snapshots: ContextSnapshotService,
        observability: ObservabilityFacade | None = None,
    ) -> None:
        self._builder = builder
        self._audits = audits
        self._snapshots = snapshots
        self._observability = observability

    def _context(self) -> ObservabilityContext:
        inherited = current_observability_context()
        return ObservabilityContext(
            correlation_id=(
                inherited.correlation_id if inherited is not None else uuid4()
            ),
            request_id=inherited.request_id if inherited is not None else None,
            run_id=inherited.run_id if inherited is not None else None,
            step_id=inherited.step_id if inherited is not None else None,
            operation_name="context.build",
            component="context",
        )

    async def build(
        self, user: UserAccount, command: ContextBuildCommand
    ) -> BuiltContext:
        if self._observability is None:
            return await self._builder.build(user.id, command)
        with self._observability.operation(
            "context.build", context=self._context()
        ) as span:
            result = await self._builder.build(user.id, command)
            if result.degraded_mode.value == "NONE":
                span.succeed()
            else:
                span.degrade(result.degraded_mode.value)
                span.succeed("DEGRADED")
            return result

    async def get_audit(self, user: UserAccount, audit_id: UUID) -> ContextBuildAudit:
        return await self._audits.get(user.id, audit_id)

    async def build_snapshot(
        self,
        user: UserAccount,
        command: ContextBuildCommand,
        *,
        scope_id: str,
    ) -> FrozenContextSnapshot:
        if self._observability is None:
            return await self._snapshots.build(
                user_id=user.id,
                command=command,
                scope_id=scope_id,
            )
        with self._observability.operation(
            "context.build_snapshot", context=self._context()
        ) as span:
            result = await self._snapshots.build(
                user_id=user.id,
                command=command,
                scope_id=scope_id,
            )
            span.succeed()
            return result

    async def get_snapshot(
        self,
        user: UserAccount,
        snapshot_id: UUID,
        *,
        validate_versions: bool = True,
    ) -> FrozenContextSnapshot:
        return await self._snapshots.get(
            user_id=user.id,
            snapshot_id=snapshot_id,
            validate_versions=validate_versions,
        )

    async def get_snapshot_reference(
        self,
        user: UserAccount,
        snapshot_id: UUID,
    ) -> ContextSnapshotReference:
        return (await self.get_snapshot(user, snapshot_id)).reference
