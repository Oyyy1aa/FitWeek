"""Bounded provider retry and deterministic fallback boundary."""

from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.context.enums import ContextDegradedMode
from app.domain.model_gateway.errors import ModelServerError
from app.domain.model_gateway.models import ModelRequest, ModelTraceContext
from app.model_gateway.fake_provider import ScriptedFakeProvider
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.limiter import ProcessLocalModelLimiter
from app.model_gateway.retry_policy import ModelRetryPolicy

pytestmark = pytest.mark.phase_5a


@pytest.mark.asyncio
async def test_gateway_never_exceeds_two_primary_and_one_backup_attempt() -> None:
    primary = ScriptedFakeProvider(
        script=(ModelServerError(), ModelServerError()),
    )
    backup = ScriptedFakeProvider(
        provider_name="backup-fake",
        script=(ModelServerError(),),
    )
    gateway = ModelGateway(
        primary=primary,
        primary_model="primary",
        backup=backup,
        backup_model="backup",
        retry_policy=ModelRetryPolicy(max_attempts=3, retry_delay_seconds=0),
        limiter=ProcessLocalModelLimiter(
            max_concurrency=1,
            rate_per_minute=100,
            acquire_timeout_seconds=1,
        ),
    )
    request_id = uuid4()
    result = await gateway.invoke(
        request=ModelRequest(
            request_id=request_id,
            model="selected",
            system_prompt="system",
            user_prompt="user",
            response_schema_name="schema",
            temperature=0,
            max_output_tokens=100,
            timeout_seconds=1,
            metadata={"agent": "session-designer"},
        ),
        trace_context=ModelTraceContext(
            user_id=uuid4(),
            agent_name="session-designer",
            prompt_name="session-designer",
            prompt_version="session-designer-v1",
            prompt_sha256="hash",
            input_fingerprint="fingerprint",
            context_degraded_mode=ContextDegradedMode.NONE,
        ),
        validator=lambda value: value,
        template_factory=lambda: "safe-template",
    )
    assert primary.call_count == 2
    assert backup.call_count == 1
    assert result.value == "safe-template"
    assert result.provider_attempts == 3
    assert result.fallback_used is True


def test_agent_module_has_no_business_repository_dependency() -> None:
    source = Path("app/agents/session_designer.py").read_text(encoding="utf-8")
    assert "Repository" not in source
    assert "app.persistence" not in source
    assert "PlanRepository" not in source
    assert "MemoryRepository" not in source
