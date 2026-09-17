"""50-case offline contract evaluation using only scripted fake outputs."""

import json
from collections import Counter
from pathlib import Path

import pytest

from app.agents.profile_agent import ProfileAgentOutOfScopeError
from tests.phase3a_helpers import (
    TEST_USER_ID,
    agent_input,
    build_agent,
    build_gateway,
    output_json,
)

pytestmark = pytest.mark.phase_3a

DATASET_PATH = Path(__file__).with_name("profile_agent_cases.json")
CASES = json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def test_evaluation_dataset_shape_and_category_counts() -> None:
    counts = Counter(case["category"] for case in CASES)
    assert len(CASES) == 50
    assert counts == {
        "complete": 10,
        "missing": 8,
        "temporary": 8,
        "long_term": 8,
        "hard": 6,
        "out_of_scope": 5,
        "ambiguous": 5,
    }
    assert sum(case["language"] == "zh" for case in CASES) >= 25


@pytest.mark.parametrize("case", CASES, ids=lambda item: item["id"])
@pytest.mark.asyncio
async def test_profile_agent_contract_case(case: dict[str, str]) -> None:
    category = case["category"]
    gateway, primary, _ = build_gateway(
        primary_script=[
            output_json(
                category=category,
                scope_status=case["expected_scope"],
            )
        ]
    )
    agent = build_agent(gateway)
    if category == "out_of_scope":
        with pytest.raises(ProfileAgentOutOfScopeError):
            await agent.run(agent_input(case["user_message"]), user_id=TEST_USER_ID)
        assert primary.call_count == 0
        return

    result = await agent.run(
        agent_input(case["user_message"]),
        user_id=TEST_USER_ID,
    )
    assert result.output.scope_status.value == case["expected_scope"]
    if category == "ambiguous":
        assert primary.call_count == 0
    elif category == "temporary":
        assert len(result.output.temporary_constraints) == 1
    elif category == "long_term":
        assert len(result.output.memory_candidates) == 1
    elif category == "hard":
        assert len(result.output.hard_constraints) == 1
    elif category == "missing":
        assert result.output.missing_fields
    else:
        assert result.output.weekly_frequency == 3
