"""Plan evaluator correctness, hard constraints, and determinism."""

import json

import pytest

from app.evaluation.models import EvaluationCaseStatus
from app.evaluation.plan_evaluator import PlanCaseEvaluator

pytestmark = pytest.mark.phase_9a


PLAN_CATEGORIES = (
    "basic_profile",
    "weekly_frequency",
    "session_duration",
    "equipment_constraint",
    "location_constraint",
    "excluded_feature",
    "multi_constraint",
    "no_available_exercise",
    "schedule_time_window",
    "calendar_busy_manual_only",
    "recovery_spacing",
    "completed_checked_in_immutable",
    "plan_version_cas_idempotency",
    "boundary_invalid",
)


@pytest.mark.parametrize("category", PLAN_CATEGORIES)
@pytest.mark.asyncio
async def test_representative_plan_category_passes(plan_cases, category: str) -> None:
    case = next(item for item in plan_cases if item.category == category)
    result = await PlanCaseEvaluator().evaluate(case)
    assert result.status is EvaluationCaseStatus.PASS
    assert not result.violations


@pytest.mark.parametrize(
    "category,metric",
    [
        ("equipment_constraint", "hard_constraint_escape"),
        ("location_constraint", "hard_constraint_escape"),
        ("excluded_feature", "hard_constraint_escape"),
        ("multi_constraint", "hard_constraint_escape"),
        ("completed_checked_in_immutable", "recovery_immutable_violation"),
        ("calendar_busy_manual_only", "calendar_side_effect_before_confirmation"),
        ("plan_version_cas_idempotency", "duplicate_calendar_event"),
    ],
)
@pytest.mark.asyncio
async def test_plan_zero_tolerance_metric_stays_zero(
    plan_cases, category, metric
) -> None:
    case = next(item for item in plan_cases if item.category == category)
    result = await PlanCaseEvaluator().evaluate(case)
    assert result.metric_values[metric] == 0


@pytest.mark.parametrize(
    "expected_code",
    [
        "NO_ELIGIBLE_EXERCISES",
        "INSUFFICIENT_AVAILABILITY",
        "AVAILABILITY_SLOT_OVERLAP",
        "SCOPE_NOT_CONFIRMED",
        "NO_VALID_TIME_SLOT",
        "DOMAIN_VALIDATION_ERROR",
    ],
)
@pytest.mark.asyncio
async def test_expected_rejection_codes_are_recognized(
    plan_cases, expected_code
) -> None:
    case = next(
        item for item in plan_cases if expected_code in item.expected_rejection_codes
    )
    result = await PlanCaseEvaluator().evaluate(case)
    assert result.status is EvaluationCaseStatus.PASS
    assert result.error_code in case.expected_rejection_codes


@pytest.mark.asyncio
async def test_plan_output_fingerprint_is_repeatable(plan_cases) -> None:
    evaluator = PlanCaseEvaluator()
    first = await evaluator.evaluate(plan_cases[0])
    second = await evaluator.evaluate(plan_cases[0])
    assert first.output_fingerprint == second.output_fingerprint
    assert first.metric_values["deterministic_output"] == 1


def test_formal_plan_result_has_all_zero_tolerance_metrics(repository_root) -> None:
    report = json.loads(
        (repository_root / "evaluation/reports/phase-9a-plan-results.json").read_text()
    )
    metrics = report["metrics"]
    assert metrics["hard_constraint_escape_count"] == 0
    assert metrics["safety_escape_count"] == 0
    assert metrics["calendar_side_effect_before_confirmation_count"] == 0
    assert metrics["duplicate_calendar_event_count"] == 0
    assert metrics["recovery_immutable_violation_count"] == 0
