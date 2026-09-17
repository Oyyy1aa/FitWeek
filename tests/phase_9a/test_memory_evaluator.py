"""Memory lifecycle, isolation, degradation, and deterministic context tests."""

import json

import pytest

from app.evaluation.memory_evaluator import MemoryCaseEvaluator
from app.evaluation.models import EvaluationCaseStatus

pytestmark = pytest.mark.phase_9a


MEMORY_CATEGORIES = (
    "active_recall",
    "pending_review_not_recalled",
    "rejected_not_recalled",
    "deleted_not_recalled",
    "expired_not_recalled",
    "ttl_boundary",
    "duplicate_candidate",
    "conflict_candidate",
    "evidence_validation",
    "no_memory_degradation",
    "user_isolation",
    "context_ordering",
)


@pytest.mark.parametrize("category", MEMORY_CATEGORIES)
@pytest.mark.asyncio
async def test_representative_memory_category_passes(memory_cases, category) -> None:
    case = next(item for item in memory_cases if item.category == category)
    result = await MemoryCaseEvaluator().evaluate(case)
    assert result.status is EvaluationCaseStatus.PASS
    assert not result.violations


@pytest.mark.parametrize(
    "category,metric,expected",
    [
        ("active_recall", "active_memory_recall_accurate", 1),
        ("pending_review_not_recalled", "pending_memory_leak", 0),
        ("expired_not_recalled", "expired_memory_recall", 0),
        ("deleted_not_recalled", "deleted_memory_recall", 0),
        ("user_isolation", "cross_user_memory_leak", 0),
        ("duplicate_candidate", "memory_candidate_idempotent", 1),
        ("conflict_candidate", "memory_conflict_detected", 1),
        ("no_memory_degradation", "no_memory_degradation_succeeded", 1),
        ("context_ordering", "context_deterministic", 1),
    ],
)
@pytest.mark.asyncio
async def test_memory_metric_matches_contract(
    memory_cases, category, metric, expected
) -> None:
    case = next(item for item in memory_cases if item.category == category)
    result = await MemoryCaseEvaluator().evaluate(case)
    assert result.metric_values[metric] == expected


@pytest.mark.asyncio
async def test_memory_output_fingerprint_is_repeatable(memory_cases) -> None:
    evaluator = MemoryCaseEvaluator()
    first = await evaluator.evaluate(memory_cases[0])
    second = await evaluator.evaluate(memory_cases[0])
    assert first.output_fingerprint == second.output_fingerprint


def test_formal_memory_result_has_no_leaks(repository_root) -> None:
    report = json.loads(
        (
            repository_root / "evaluation/reports/phase-9a-memory-results.json"
        ).read_text()
    )
    metrics = report["metrics"]
    assert metrics["cross_user_memory_leak_count"] == 0
    assert metrics["pending_memory_leak_count"] == 0
    assert metrics["expired_memory_recall_count"] == 0
    assert metrics["deleted_memory_recall_count"] == 0
