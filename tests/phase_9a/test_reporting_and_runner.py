"""Safe reports, CLI behavior, filters, and evaluation observability descriptors."""

import json

import pytest

from app.evaluation.reporter import EvaluationReportError, render_json
from app.evaluation.runner import EXIT_DATASET_INVALID, EXIT_PASSED, async_main
from app.observability.metrics import DEFAULT_METRIC_DESCRIPTORS

pytestmark = pytest.mark.phase_9a


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "email",
        "password",
        "authorization",
        "api_key",
        "user_message",
        "prompt",
        "raw_model_response",
        "memory_value",
        "calendar_event",
        "traceback",
    ],
)
def test_reporter_rejects_forbidden_fields(forbidden_key: str) -> None:
    with pytest.raises(EvaluationReportError):
        render_json({forbidden_key: "synthetic"})


def test_reporter_uses_stable_sorted_json() -> None:
    assert render_json({"b": 2, "a": 1}).startswith('{\n  "a": 1')


@pytest.mark.parametrize(
    "metric_name",
    [
        "fitweek_evaluation_cases_total",
        "fitweek_evaluation_failures_total",
        "fitweek_evaluation_duration_seconds",
        "fitweek_evaluation_gate_failures_total",
    ],
)
def test_evaluation_metric_descriptor_exists(metric_name: str) -> None:
    descriptor = next(
        item for item in DEFAULT_METRIC_DESCRIPTORS if item.name == metric_name
    )
    assert "case_id" not in descriptor.labels
    descriptor.validate()


@pytest.mark.asyncio
async def test_runner_supports_single_case(tmp_path) -> None:
    output = tmp_path / "single.json"
    code = await async_main(
        ["--dataset", "plan", "--case-id", "PLAN-001", "--output", str(output)]
    )
    assert code == EXIT_PASSED
    assert json.loads(output.read_text())["total"] == 1


@pytest.mark.asyncio
async def test_runner_supports_tag_filter(tmp_path) -> None:
    output = tmp_path / "tag.json"
    code = await async_main(
        ["--dataset", "memory", "--tag", "active_recall", "--output", str(output)]
    )
    assert code == EXIT_PASSED
    assert json.loads(output.read_text())["total"] == 15


@pytest.mark.asyncio
async def test_runner_rejects_unknown_case(tmp_path) -> None:
    code = await async_main(
        [
            "--dataset",
            "plan",
            "--case-id",
            "PLAN-999",
            "--output",
            str(tmp_path / "x.json"),
        ]
    )
    assert code == EXIT_DATASET_INVALID


@pytest.mark.parametrize(
    "result_file,expected_total",
    [
        ("phase-9a-plan-results.json", 200),
        ("phase-9a-memory-results.json", 108),
    ],
)
def test_formal_result_is_complete_and_unskipped(
    repository_root, result_file, expected_total
) -> None:
    report = json.loads(
        (repository_root / "evaluation/reports" / result_file).read_text()
    )
    assert report["total"] == report["passed"] == expected_total
    assert report["failed"] == report["skipped"] == report["timed_out"] == 0
    assert report["internal_error"] == 0


def test_summary_references_all_machine_results(repository_root) -> None:
    summary = json.loads(
        (repository_root / "evaluation/reports/phase-9a-summary.json").read_text()
    )
    assert len(summary["ablation_modes"]) == 6
    assert summary["zero_tolerance_passed"] is True
