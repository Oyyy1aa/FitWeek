"""Bounded retry, backup, fallback, trace, metric, and limiter tests."""

import asyncio
from uuid import uuid4

import pytest

from app.domain.model_gateway.errors import (
    ModelAuthenticationError,
    ModelProviderUnavailableError,
    ModelSchemaInvalidError,
    ModelServerError,
    ModelTimeoutError,
)
from app.domain.model_gateway.models import ModelRequest, ModelTraceContext
from app.model_gateway.limiter import ProcessLocalModelLimiter
from tests.phase3a_helpers import build_gateway

pytestmark = pytest.mark.phase_3a


def request_and_context() -> tuple[ModelRequest, ModelTraceContext]:
    request_id = uuid4()
    return (
        ModelRequest(
            request_id=request_id,
            model="selected-by-gateway",
            system_prompt="system",
            user_prompt="user",
            response_schema_name="test",
            temperature=0,
            max_output_tokens=100,
            timeout_seconds=1,
            metadata={"agent": "test"},
        ),
        ModelTraceContext(
            user_id=uuid4(),
            agent_name="test-agent",
            prompt_name="test-prompt",
            prompt_version="v1",
            prompt_sha256="a" * 64,
            input_fingerprint="b" * 64,
        ),
    )


async def invoke(gateway: object) -> object:
    request, context = request_and_context()
    return await gateway.invoke(
        request=request,
        trace_context=context,
        validator=lambda raw: raw,
        template_factory=lambda: "template",
    )


@pytest.mark.asyncio
async def test_primary_first_attempt_success() -> None:
    gateway, primary, _ = build_gateway(primary_script=['{"ok":true}'])
    result = await invoke(gateway)

    assert result.value == '{"ok":true}'
    assert result.fallback_used is False
    assert primary.call_count == 1


@pytest.mark.asyncio
async def test_retryable_primary_failure_retries_once() -> None:
    gateway, primary, _ = build_gateway(
        primary_script=[ModelTimeoutError(), '{"ok":true}']
    )
    result = await invoke(gateway)

    assert result.fallback_used is False
    assert primary.call_count == 2


@pytest.mark.asyncio
async def test_primary_failure_then_backup_success() -> None:
    gateway, primary, backup = build_gateway(
        primary_script=[ModelServerError(), ModelServerError()],
        backup_script=['{"backup":true}'],
    )
    result = await invoke(gateway)

    assert result.provider_name == "backup-fake"
    assert primary.call_count == 2
    assert backup is not None and backup.call_count == 1


@pytest.mark.asyncio
async def test_authentication_error_is_not_retried_but_backup_is_allowed() -> None:
    gateway, primary, backup = build_gateway(
        primary_script=[ModelAuthenticationError()],
        backup_script=['{"backup":true}'],
    )
    result = await invoke(gateway)

    assert result.provider_name == "backup-fake"
    assert primary.call_count == 1
    assert backup is not None and backup.call_count == 1


@pytest.mark.asyncio
async def test_all_providers_fail_uses_fixed_template_with_three_attempt_cap() -> None:
    gateway, primary, backup = build_gateway(
        primary_script=[ModelServerError(), ModelServerError()],
        backup_script=[ModelServerError()],
    )
    request, context = request_and_context()
    result = await gateway.invoke(
        request=request,
        trace_context=context,
        validator=lambda raw: raw,
        template_factory=lambda: "safe-template",
    )
    traces = await gateway.list_traces(
        user_id=context.user_id,
        request_id=request.request_id,
    )

    assert result.value == "safe-template"
    assert result.fallback_used is True
    assert result.provider_attempts == 3
    assert primary.call_count == 2
    assert backup is not None and backup.call_count == 1
    assert len(traces) == 4
    assert traces[-1].provider_name == "template-fallback"
    assert traces[-1].input_tokens is None


@pytest.mark.asyncio
async def test_contract_failure_moves_to_backup_without_primary_retry() -> None:
    gateway, primary, backup = build_gateway(
        primary_script=["invalid-contract"],
        backup_script=['{"ok":true}'],
    )
    request, context = request_and_context()

    def validator(raw: str) -> str:
        if raw == "invalid-contract":
            raise ModelSchemaInvalidError()
        return raw

    result = await gateway.invoke(
        request=request,
        trace_context=context,
        validator=validator,
        template_factory=lambda: "template",
    )

    assert result.provider_name == "backup-fake"
    assert primary.call_count == 1
    assert backup is not None and backup.call_count == 1


@pytest.mark.asyncio
async def test_metrics_track_attempts_errors_successes_and_fallbacks() -> None:
    gateway, _, _ = build_gateway(
        primary_script=[ModelTimeoutError(), ModelTimeoutError()]
    )
    await invoke(gateway)
    metrics = gateway.metrics()

    assert metrics.requests_total == 1
    assert metrics.provider_attempts_total == 2
    assert metrics.timeouts >= 2
    assert metrics.template_fallbacks == 1
    assert metrics.requests_in_flight == 0


@pytest.mark.asyncio
async def test_process_local_rate_limit_and_bounded_concurrency_wait() -> None:
    times = iter((0.0, 0.0, 0.0))
    limiter = ProcessLocalModelLimiter(
        max_concurrency=1,
        rate_per_minute=2,
        acquire_timeout_seconds=0.02,
        clock=lambda: next(times),
    )
    first_entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with limiter.slot():
            first_entered.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await first_entered.wait()
    with pytest.raises(ModelProviderUnavailableError, match="queue timed out"):
        async with limiter.slot():
            pass
    release.set()
    await task
