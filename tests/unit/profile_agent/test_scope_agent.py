"""Pre-provider scope, strict output, and no-write Agent contracts."""

import json

import pytest

from app.agents.profile_agent import ProfileAgentOutOfScopeError
from app.domain.profile_agent.models import ScopeStatus
from app.domain.profile_agent.scope import DeterministicScopeGuard
from tests.phase3a_helpers import (
    TEST_USER_ID,
    agent_input,
    build_agent,
    build_gateway,
    output_document,
)

pytestmark = pytest.mark.phase_3a


@pytest.mark.parametrize(
    "message",
    [
        "我训练时胸痛，需要调整处方",
        "术后恢复训练怎么安排",
        "Please create a post-operative rehabilitation plan",
        "CARDIAC exercise prescription for chest pain",
        "pregnancy training program",
    ],
)
def test_explicit_medical_requests_are_out_of_scope(message: str) -> None:
    assert (
        DeterministicScopeGuard().classify(message).status is ScopeStatus.OUT_OF_SCOPE
    )


@pytest.mark.parametrize(
    "message",
    [
        "最近膝盖不舒服",
        "运动时头晕",
        "I might be injured",
        "knee discomfort during training",
    ],
)
def test_ambiguous_health_risk_needs_review(message: str) -> None:
    assert (
        DeterministicScopeGuard().classify(message).status is ScopeStatus.NEEDS_REVIEW
    )


@pytest.mark.asyncio
async def test_out_of_scope_and_ambiguous_requests_do_not_call_provider() -> None:
    gateway, primary, _ = build_gateway()
    agent = build_agent(gateway)

    with pytest.raises(ProfileAgentOutOfScopeError):
        await agent.run(agent_input("胸痛"), user_id=TEST_USER_ID)
    ambiguous = await agent.run(
        agent_input("最近膝盖不舒服"),
        user_id=TEST_USER_ID,
    )

    assert primary.call_count == 0
    assert ambiguous.output.scope_status is ScopeStatus.NEEDS_REVIEW
    assert gateway.metrics().scope_guard_blocks == 1


@pytest.mark.asyncio
async def test_valid_output_is_parsed_and_prompt_summary_is_redacted() -> None:
    gateway, primary, _ = build_gateway()
    agent = build_agent(gateway)
    result = await agent.run(agent_input(), user_id=TEST_USER_ID)

    assert result.output.scope_status is ScopeStatus.SUPPORTED
    assert result.fallback_used is False
    assert primary.call_count == 1
    summary = primary.request_summaries[0].values
    assert "user_message" not in summary
    assert "system_prompt" not in summary


@pytest.mark.asyncio
async def test_unknown_catalog_value_cannot_bypass_business_validation() -> None:
    invalid = output_document()
    invalid["equipment"] = ["unsupported_device"]
    gateway, primary, _ = build_gateway(
        primary_script=[json.dumps(invalid, ensure_ascii=False)]
    )
    result = await build_agent(gateway).run(agent_input(), user_id=TEST_USER_ID)

    assert primary.call_count == 1
    assert result.fallback_used is True
    assert result.output.scope_status is ScopeStatus.NEEDS_REVIEW
    assert gateway.metrics().business_validation_failures >= 1


@pytest.mark.asyncio
async def test_extra_fields_and_unknown_goals_fail_schema_without_coercion() -> None:
    invalid = output_document()
    invalid["weekly_plan"] = {"sessions": []}
    invalid["goals"] = ["PRO_ATHLETE"]
    gateway, _, _ = build_gateway(
        primary_script=[json.dumps(invalid, ensure_ascii=False)]
    )
    result = await build_agent(gateway).run(agent_input(), user_id=TEST_USER_ID)

    assert result.fallback_used is True
    assert gateway.metrics().schema_failures >= 1


def test_input_rejects_empty_long_and_non_monday_values() -> None:
    with pytest.raises(ValueError):
        agent_input("")
    with pytest.raises(ValueError):
        agent_input("x" * 4001)
