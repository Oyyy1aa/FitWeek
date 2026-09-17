"""Bounded Calendar write gateway with retry classification and safe traces."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from app.domain.calendar_operations.enums import CalendarAttemptOutcome
from app.domain.calendar_operations.models import (
    CalendarEventPayload,
    CalendarProviderResult,
)
from app.domain.calendar_operations.protocols import CalendarWriteProvider
from app.domain.tools.enums import (
    ToolCaller,
    ToolErrorCategory,
    ToolId,
    ToolInvocationStatus,
)
from app.domain.tools.models import ToolInvocationContext
from app.tool_adapters.contracts import CalendarCommitRequest, CalendarCommitResponse
from app.tool_gateway.gateway import ToolGateway


class CalendarProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarGatewayAttempt:
    attempt_no: int
    outcome: CalendarAttemptOutcome
    error_code: str | None
    response_reference_hash: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarGatewayResult:
    succeeded: bool
    provider_result: CalendarProviderResult | None
    attempts: tuple[CalendarGatewayAttempt, ...]
    retryable: bool


class CalendarWriteGateway:
    def __init__(
        self,
        *,
        provider: CalendarWriteProvider | None,
        enabled: bool,
        timeout_seconds: float,
        max_attempts: int,
        tool_gateway: ToolGateway | None = None,
        user_id: UUID | None = None,
    ) -> None:
        self._provider = provider
        self._enabled = enabled
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        if tool_gateway is None and enabled and provider is not None:
            # Compatibility facade: direct construction remains routed through the
            # same gateway; the facade owns no retry, deadline, or circuit policy.
            from app.tool_gateway.factory import build_tool_gateway

            tool_gateway = build_tool_gateway(
                calendar_write_provider=provider,
                calendar_write_timeout_ms=max(1, int(timeout_seconds * 1000)),
                calendar_write_attempts=max_attempts,
            )
        self._tool_gateway = tool_gateway
        self._user_id = user_id or UUID(int=0)

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name if self._provider else "none"

    async def create(
        self,
        *,
        calendar_id: str,
        operation_key: str,
        payload: CalendarEventPayload,
        correlation_id: UUID | None = None,
        run_id: UUID | None = None,
        step_id: UUID | None = None,
    ) -> CalendarGatewayResult:
        return await self._execute_tool(
            CalendarCommitRequest(
                calendar_id=calendar_id,
                operation_key=operation_key,
                operation="CREATE",
                payload=payload,
            ),
            correlation_id=correlation_id,
            run_id=run_id,
            step_id=step_id,
        )

    async def update(
        self,
        *,
        calendar_id: str,
        external_event_id: str,
        operation_key: str,
        payload: CalendarEventPayload,
        correlation_id: UUID | None = None,
        run_id: UUID | None = None,
        step_id: UUID | None = None,
    ) -> CalendarGatewayResult:
        return await self._execute_tool(
            CalendarCommitRequest(
                calendar_id=calendar_id,
                external_event_id=external_event_id,
                operation_key=operation_key,
                operation="UPDATE",
                payload=payload,
            ),
            correlation_id=correlation_id,
            run_id=run_id,
            step_id=step_id,
        )

    async def delete(
        self,
        *,
        calendar_id: str,
        external_event_id: str,
        operation_key: str,
        correlation_id: UUID | None = None,
        run_id: UUID | None = None,
        step_id: UUID | None = None,
    ) -> CalendarGatewayResult:
        return await self._execute_tool(
            CalendarCommitRequest(
                calendar_id=calendar_id,
                external_event_id=external_event_id,
                operation_key=operation_key,
                operation="DELETE",
            ),
            correlation_id=correlation_id,
            run_id=run_id,
            step_id=step_id,
        )

    async def _execute_tool(
        self,
        request: CalendarCommitRequest,
        *,
        correlation_id: UUID | None,
        run_id: UUID | None,
        step_id: UUID | None,
    ) -> CalendarGatewayResult:
        if not self._enabled or self._provider is None:
            return CalendarGatewayResult(
                succeeded=False,
                provider_result=None,
                attempts=(),
                retryable=False,
            )
        if self._tool_gateway is None:
            raise RuntimeError(
                "CalendarWriteGateway requires ToolGateway when enabled."
            )
        now = self._tool_gateway.clock.now()
        outcome = await self._tool_gateway.invoke(
            ToolInvocationContext(
                invocation_id=uuid4(),
                correlation_id=correlation_id or uuid4(),
                user_id=self._user_id,
                caller=ToolCaller.CALENDAR_EXECUTOR,
                tool_id=ToolId.CALENDAR_COMMIT,
                tool_version="phase-8a-v1",
                deadline_at=now + timedelta(seconds=self._timeout * self._max_attempts),
                created_at=now,
                run_id=run_id,
                step_id=step_id,
                idempotency_key=request.operation_key,
            ),
            request,
        )
        response = outcome.response
        commit_response = (
            response if isinstance(response, CalendarCommitResponse) else None
        )
        success = (
            outcome.result.status is ToolInvocationStatus.SUCCEEDED
            and commit_response is not None
        )
        if success:
            assert commit_response is not None
        response_reference = (
            commit_response.response_reference if commit_response is not None else None
        )
        attempts = tuple(
            CalendarGatewayAttempt(
                attempt_no=index,
                outcome=(
                    CalendarAttemptOutcome.SUCCEEDED
                    if success and index == outcome.result.attempt_count
                    else CalendarAttemptOutcome.FAILED_RETRYABLE
                ),
                error_code=(
                    None
                    if success and index == outcome.result.attempt_count
                    else outcome.result.error_code
                ),
                response_reference_hash=(
                    response_reference
                    if success and index == outcome.result.attempt_count
                    else None
                ),
            )
            for index in range(1, outcome.result.attempt_count + 1)
        )
        return CalendarGatewayResult(
            succeeded=success,
            provider_result=(
                CalendarProviderResult(
                    external_event_id=commit_response.external_event_id,
                    response_reference_hash=commit_response.response_reference,
                )
                if success and commit_response is not None
                else None
            ),
            attempts=attempts,
            retryable=(
                outcome.result.status is ToolInvocationStatus.FAILED_RETRYABLE
                or outcome.result.error_category
                in {
                    ToolErrorCategory.CIRCUIT_OPEN,
                    ToolErrorCategory.BULKHEAD_REJECTED,
                    ToolErrorCategory.DEADLINE_EXCEEDED,
                }
            ),
        )

    async def close(self) -> None:
        if self._provider is not None:
            await self._provider.close()
