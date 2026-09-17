"""Bounded read-only Calendar gateway with MANUAL_ONLY degradation."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from app.domain.calendar_read.models import CalendarBusyInterval, CalendarReadRequest
from app.domain.calendar_read.protocols import CalendarReadProvider
from app.domain.scheduling.enums import CalendarReadMode
from app.domain.tools.enums import (
    ToolCaller,
    ToolDegradationMode,
    ToolErrorCategory,
    ToolId,
    ToolInvocationStatus,
)
from app.domain.tools.models import ToolInvocationContext
from app.tool_adapters.contracts import (
    CalendarFreeBusyRequest,
    CalendarFreeBusyResponse,
)
from app.tool_gateway.gateway import ToolGateway
from app.tool_gateway.traces import ReliabilityCompatibilityTrace


class CalendarReadError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarReadResult:
    intervals: tuple[CalendarBusyInterval, ...]
    mode: CalendarReadMode
    provider_summary: str
    attempts: int


class CalendarReadGateway:
    def __init__(
        self,
        *,
        provider: CalendarReadProvider | None,
        enabled: bool,
        timeout_seconds: float = 3,
        max_attempts: int = 2,
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
                calendar_read_provider=provider,
                calendar_read_timeout_ms=max(1, int(timeout_seconds * 1000)),
                calendar_read_attempts=max_attempts,
            )
        self._tool_gateway = tool_gateway
        self._user_id = user_id or UUID(int=0)

    async def read_busy(self, request: CalendarReadRequest) -> CalendarReadResult:
        if not self._enabled:
            return CalendarReadResult(
                intervals=(),
                mode=CalendarReadMode.DISABLED,
                provider_summary="calendar-read-disabled",
                attempts=0,
            )
        if self._provider is None:
            return CalendarReadResult(
                intervals=(),
                mode=CalendarReadMode.MANUAL_ONLY,
                provider_summary="calendar-provider-unavailable",
                attempts=0,
            )
        if self._tool_gateway is None:
            raise RuntimeError(
                "CalendarReadGateway requires ToolGateway when calendar reading is "
                "enabled."
            )
        now = self._tool_gateway.clock.now()
        outcome = await self._tool_gateway.invoke(
            ToolInvocationContext(
                invocation_id=uuid4(),
                correlation_id=uuid4(),
                user_id=self._user_id,
                caller=ToolCaller.SCHEDULE_APPLICATION,
                tool_id=ToolId.CALENDAR_FREE_BUSY,
                tool_version="phase-8a-v1",
                deadline_at=now + timedelta(seconds=self._timeout * self._max_attempts),
                created_at=now,
            ),
            CalendarFreeBusyRequest(
                start=request.start, end=request.end, timezone=request.timezone
            ),
        )
        if outcome.result.status is ToolInvocationStatus.SUCCEEDED and isinstance(
            outcome.response, CalendarFreeBusyResponse
        ):
            return CalendarReadResult(
                intervals=tuple(
                    CalendarBusyInterval(start=item.start, end=item.end)
                    for item in outcome.response.busy
                ),
                mode=CalendarReadMode.PROVIDER,
                provider_summary=f"{self._provider.provider_name}:{self._provider.provider_version}",
                attempts=outcome.result.attempt_count,
            )
        return CalendarReadResult(
            intervals=(),
            mode=CalendarReadMode.MANUAL_ONLY,
            provider_summary=f"{self._provider.provider_name}:degraded",
            attempts=outcome.result.attempt_count,
        )

    async def close(self) -> None:
        if self._provider is not None:
            await self._provider.close()

    def record_stale_rejection(self, *, user_id: UUID, code: str) -> None:
        """Record stale rejection without turning it into degradation success."""

        if self._tool_gateway is None:
            return
        now = self._tool_gateway.clock.now()
        self._tool_gateway.metrics.record_stale_data_rejected()
        self._tool_gateway.traces.add_compatibility(
            ReliabilityCompatibilityTrace(
                correlation_id=uuid4(),
                user_id=user_id,
                component="CALENDAR_STALE_REVALIDATION",
                attempt_no=1,
                status="REJECTED",
                error_category=ToolErrorCategory.STALE_DATA,
                error_code=code,
                degradation_mode=ToolDegradationMode.STALE_DATA_REJECTED,
                created_at=now,
            )
        )
