"""80-case offline Recovery Agent contract evaluation; never live quality."""

import json
from collections import Counter
from pathlib import Path

import pytest

from app.domain.recovery.enums import RecoveryScopeStatus
from app.recovery.scope_guard import RecoveryScopeGuard

pytestmark = pytest.mark.phase_7a

CASES = json.loads(
    (Path(__file__).with_name("recovery_agent_cases.json")).read_text(encoding="utf-8")
)

EXPECTED_COUNTS = {
    "supported_request": 8,
    "out_of_scope_zh": 10,
    "out_of_scope_en": 8,
    "needs_review": 8,
    "candidate_id_only": 6,
    "invalid_candidate": 6,
    "conflicting_action": 8,
    "immutable_target": 6,
    "skipped_history": 6,
    "provider_failure": 6,
    "prompt_injection": 4,
    "temporary_request": 4,
}


def test_recovery_evaluation_dataset_has_exactly_80_unique_cases() -> None:
    assert len(CASES) == 80
    assert len({item["id"] for item in CASES}) == 80
    assert Counter(item["category"] for item in CASES) == EXPECTED_COUNTS
    assert all(item["user_request"].strip() for item in CASES)


@pytest.mark.parametrize("case", CASES, ids=[item["id"] for item in CASES])
def test_recovery_scope_contract(case: dict[str, str]) -> None:
    result = RecoveryScopeGuard().evaluate(case["user_request"])
    assert result.status is RecoveryScopeStatus(case["expected_scope"])


def test_evaluation_is_explicitly_offline_contract_not_semantic_quality() -> None:
    assert all("expected_scope" in item for item in CASES)
    assert all("live_model_score" not in item for item in CASES)
