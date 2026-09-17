"""Deterministic, side-effect-free ICS export use case."""

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from app.application.errors import (
    IcsExportIdempotencyConflict,
    IcsExportNotFound,
    IcsPlanNotConfirmed,
    IcsPlanRevisionNotCurrent,
    IcsSerializationFailed,
)
from app.domain.common import RepositoryUniqueError, require_non_blank
from app.domain.ics.models import IcsExportRecord, IcsExportResult
from app.domain.ics.repositories import IcsExportRepository
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.tools.enums import ToolCaller, ToolId, ToolInvocationStatus
from app.domain.tools.models import ToolInvocationContext
from app.domain.users.models import UserAccount
from app.ics.builder import IcsBuilder
from app.orchestration.clock import Clock
from app.tool_adapters.contracts import IcsExportRequest, IcsExportResponse
from app.tool_gateway.gateway import ToolGateway


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateIcsExportCommand:
    client_request_id: str
    expected_plan_version: int

    def __post_init__(self) -> None:
        require_non_blank(self.client_request_id, "client_request_id")
        if self.expected_plan_version < 1:
            raise ValueError("expected_plan_version must be positive")


class IcsExportService:
    def __init__(
        self,
        *,
        plans: PlanRepository,
        exports: IcsExportRepository,
        clock: Clock,
        builder: IcsBuilder | None = None,
        tool_gateway: ToolGateway | None = None,
    ) -> None:
        self._plans = plans
        self._exports = exports
        self._clock = clock
        self._builder = builder or IcsBuilder()
        self._tools = tool_gateway

    async def create(
        self,
        user: UserAccount,
        root_plan_id: UUID,
        revision: int,
        command: CreateIcsExportCommand,
    ) -> tuple[IcsExportRecord, bool]:
        plan = await self._plans.get_revision_for_user(root_plan_id, user.id, revision)
        if plan is None or plan.status is not WeeklyPlanStatus.CONFIRMED:
            raise IcsPlanNotConfirmed("Only a confirmed Plan Revision can be exported.")
        current = await self._plans.get_current_confirmed(root_plan_id, user.id)
        if current is None or current.id != plan.id:
            raise IcsPlanRevisionNotCurrent(
                "Only the current confirmed Plan Revision can be exported."
            )
        if plan.version != command.expected_plan_version:
            raise IcsPlanRevisionNotCurrent("The Plan version is stale.")
        fingerprint = self._fingerprint(user.id, plan.id, plan.revision, plan.version)
        existing = await self._exports.get_by_request(
            user.id, command.client_request_id
        )
        if existing is not None:
            if existing.result.request_fingerprint != fingerprint:
                raise IcsExportIdempotencyConflict(
                    "The export request ID was used with different inputs."
                )
            return existing, False
        try:
            if self._tools is None:
                content, event_count = self._builder.build(
                    user_id=user.id, plan=plan, as_of=self._clock.now()
                )
            else:
                now = self._tools.clock.now()
                outcome = await self._tools.invoke(
                    ToolInvocationContext(
                        invocation_id=uuid5(NAMESPACE_URL, f"ics:{fingerprint}"),
                        correlation_id=uuid5(
                            NAMESPACE_URL, f"ics-correlation:{fingerprint}"
                        ),
                        user_id=user.id,
                        caller=ToolCaller.ICS_EXPORT_SERVICE,
                        tool_id=ToolId.ICS_EXPORT,
                        tool_version="phase-8a-v1",
                        deadline_at=now + timedelta(seconds=2),
                        created_at=now,
                        idempotency_key=command.client_request_id,
                    ),
                    IcsExportRequest(user_id=str(user.id), plan=plan, as_of=now),
                )
                if (
                    outcome.result.status is not ToolInvocationStatus.SUCCEEDED
                    or not isinstance(outcome.response, IcsExportResponse)
                ):
                    raise IcsSerializationFailed("The ICS content could not be built.")
                content, event_count = (
                    outcome.response.content,
                    outcome.response.event_count,
                )
        except (UnicodeError, ValueError) as exc:
            raise IcsSerializationFailed("The ICS content could not be built.") from exc
        content_hash = hashlib.sha256(content).hexdigest()
        export_id = uuid5(
            NAMESPACE_URL,
            f"fitweek:ics:{user.id}:{command.client_request_id}:{fingerprint}",
        )
        filename = f"fitweek-{str(root_plan_id).replace('-', '')[:12]}-r{revision}.ics"
        record = IcsExportRecord(
            result=IcsExportResult(
                id=export_id,
                user_id=user.id,
                client_request_id=command.client_request_id,
                request_fingerprint=fingerprint,
                root_plan_id=root_plan_id,
                revision=revision,
                plan_version=plan.version,
                policy_version=self._builder.policy_version,
                content_sha256=content_hash,
                event_count=event_count,
                byte_size=len(content),
                filename=filename,
                created_at=self._clock.now(),
            ),
            content=content,
        )
        try:
            saved = await self._exports.save(record)
        except RepositoryUniqueError as exc:
            raise IcsExportIdempotencyConflict(
                "The export request ID conflicts with an existing export."
            ) from exc
        return saved, True

    async def get(self, user: UserAccount, export_id: UUID) -> IcsExportRecord:
        value = await self._exports.get(user.id, export_id)
        if value is None:
            raise IcsExportNotFound("The ICS export was not found.")
        return value

    @staticmethod
    def _fingerprint(user_id: UUID, plan_id: UUID, revision: int, version: int) -> str:
        payload = {
            "user_id": str(user_id),
            "plan_id": str(plan_id),
            "revision": revision,
            "version": version,
            "policy": IcsBuilder.policy_version,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
