from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app.domain.tools.enums import (
    CircuitState,
    ToolCaller,
    ToolErrorCategory,
    ToolId,
    ToolInvocationStatus,
    ToolSideEffectClass,
)
from app.domain.tools.errors import ToolError
from app.domain.tools.models import ToolDescriptor, ToolInvocationContext
from app.reliability.clock import MutableClock
from app.reliability.sleeper import RecordingSleeper
from app.tool_gateway.circuit_breaker import CircuitBreaker
from app.tool_gateway.gateway import ToolGateway
from app.tool_gateway.registry import ToolRegistry

pytestmark = pytest.mark.phase_8a


class Request(BaseModel):
    value: str


class Response(BaseModel):
    value: str


class Adapter:
    def __init__(self, failures: list[ToolErrorCategory] | None = None) -> None:
        self.failures = list(failures or [])
        self.calls = 0

    async def invoke_once(
        self, context: ToolInvocationContext, request: Request
    ) -> Response:
        self.calls += 1
        if self.failures:
            category = self.failures.pop(0)
            raise ToolError(category, f"TEST_{category.value}")
        return Response(value=request.value)

    async def close(self) -> None:
        return None


def descriptor(
    *,
    effect: ToolSideEffectClass = ToolSideEffectClass.READ_ONLY,
    callers: frozenset[ToolCaller] | None = None,
    attempts: int = 2,
    circuit: bool = True,
    idempotency: bool = False,
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId.CALENDAR_FREE_BUSY,
        version="test-v1",
        side_effect_class=effect,
        request_model=Request,
        response_model=Response,
        default_timeout_ms=1000,
        request_deadline_budget_ms=2000,
        max_attempts=attempts,
        circuit_breaker_enabled=circuit,
        bulkhead_limit=1,
        supports_degradation=True,
        requires_idempotency_key=idempotency,
        allowed_callers=callers or frozenset({ToolCaller.SCHEDULE_APPLICATION}),
        provider_name="test-calendar",
        operation_class="read",
    )


def context(clock: MutableClock, **changes: object) -> ToolInvocationContext:
    values: dict[str, object] = {
        "invocation_id": uuid4(),
        "correlation_id": uuid4(),
        "user_id": uuid4(),
        "caller": ToolCaller.SCHEDULE_APPLICATION,
        "tool_id": ToolId.CALENDAR_FREE_BUSY,
        "tool_version": "test-v1",
        "deadline_at": clock.now() + timedelta(seconds=2),
        "created_at": clock.now(),
    }
    values.update(changes)
    return ToolInvocationContext.model_validate(values)


def gateway(adapter: Adapter, clock: MutableClock) -> ToolGateway:
    registry = ToolRegistry()
    registry.register(descriptor(), adapter)
    return ToolGateway(
        registry=registry,
        clock=clock,
        sleeper=RecordingSleeper(),
        circuits=CircuitBreaker(clock, failure_threshold=2, open_duration_seconds=10),
    )


@pytest.mark.asyncio
async def test_registry_is_explicit_typed_and_rejects_duplicate() -> None:
    registry = ToolRegistry()
    adapter = Adapter()
    registry.register(descriptor(), adapter)
    with pytest.raises(ValueError):
        registry.register(descriptor(), adapter)
    assert registry.get(ToolId.CALENDAR_FREE_BUSY.value, "test-v1").adapter is adapter
    with pytest.raises(LookupError):
        registry.get("NOT_REGISTERED", "v1")


@pytest.mark.asyncio
async def test_permission_denied_prevents_adapter_call() -> None:
    clock = MutableClock(datetime(2026, 7, 21, tzinfo=UTC))
    adapter = Adapter()
    outcome = await gateway(adapter, clock).invoke(
        context(clock, caller=ToolCaller.PROFILE_APPLICATION), Request(value="x")
    )
    assert outcome.result.error_code == "TOOL_PERMISSION_DENIED"
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_retry_is_gateway_owned_and_authentication_is_not_retried() -> None:
    clock = MutableClock(datetime(2026, 7, 21, tzinfo=UTC))
    transient = Adapter([ToolErrorCategory.UPSTREAM_5XX])
    success = await gateway(transient, clock).invoke(context(clock), Request(value="x"))
    assert success.result.status is ToolInvocationStatus.SUCCEEDED
    assert transient.calls == 2
    auth = Adapter([ToolErrorCategory.AUTHENTICATION])
    rejected = await gateway(auth, clock).invoke(context(clock), Request(value="x"))
    assert rejected.result.status is ToolInvocationStatus.FAILED_PERMANENT
    assert auth.calls == 1


@pytest.mark.asyncio
async def test_deadline_rejects_without_adapter_call() -> None:
    clock = MutableClock(datetime(2026, 7, 21, tzinfo=UTC))
    adapter = Adapter()
    result = await gateway(adapter, clock).invoke(
        context(clock, deadline_at=clock.now()), Request(value="x")
    )
    assert result.result.error_code == "TOOL_DEADLINE_EXCEEDED"
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_circuit_opens_then_half_open_probe_closes() -> None:
    clock = MutableClock(datetime(2026, 7, 21, tzinfo=UTC))
    adapter = Adapter([ToolErrorCategory.UPSTREAM_5XX] * 4)
    tool_gateway = gateway(adapter, clock)
    first = await tool_gateway.invoke(context(clock), Request(value="x"))
    assert first.result.status is ToolInvocationStatus.FAILED_RETRYABLE
    second = await tool_gateway.invoke(context(clock), Request(value="x"))
    assert second.result.degradation_mode.value == "MANUAL_ONLY"
    calls_before = adapter.calls
    third = await tool_gateway.invoke(context(clock), Request(value="x"))
    assert third.result.error_category is ToolErrorCategory.CIRCUIT_OPEN
    assert adapter.calls == calls_before
    clock.advance(timedelta(seconds=10))
    adapter.failures.clear()
    probe = await tool_gateway.invoke(context(clock), Request(value="x"))
    assert probe.result.status is ToolInvocationStatus.SUCCEEDED
    assert probe.result.circuit_state_after is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_write_tool_requires_idempotency_key() -> None:
    clock = MutableClock(datetime(2026, 7, 21, tzinfo=UTC))
    registry = ToolRegistry()
    adapter = Adapter()
    registry.register(
        descriptor(
            effect=ToolSideEffectClass.EXTERNAL_WRITE,
            callers=frozenset({ToolCaller.CALENDAR_EXECUTOR}),
            idempotency=True,
        ),
        adapter,
    )
    result = await ToolGateway(
        registry=registry, clock=clock, sleeper=RecordingSleeper()
    ).invoke(context(clock, caller=ToolCaller.CALENDAR_EXECUTOR), Request(value="x"))
    assert result.result.error_code == "TOOL_IDEMPOTENCY_KEY_REQUIRED"
    assert adapter.calls == 0
