"""Command-line Phase 9A evaluation runner with explicit exit codes."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, uuid5

import yaml  # type: ignore[import-untyped]

from app.evaluation.ablations import ablation_is_safe, load_ablations
from app.evaluation.dataset_loader import (
    DatasetValidationError,
    file_sha256,
    load_memory_cases,
    load_plan_cases,
    verify_manifest,
)
from app.evaluation.gates import all_gates_pass, evaluate_gates, load_gates
from app.evaluation.memory_evaluator import MemoryCaseEvaluator
from app.evaluation.metrics import memory_metrics, plan_metrics
from app.evaluation.models import (
    AblationComparison,
    AblationMode,
    EvaluationCaseBase,
    EvaluationCaseResult,
    EvaluationCaseStatus,
    EvaluationRunReport,
    GateCheck,
)
from app.evaluation.plan_evaluator import PlanCaseEvaluator
from app.evaluation.reporter import write_json
from app.observability.context import ObservabilityContext
from app.observability.facade import build_observability

EXIT_PASSED = 0
EXIT_GATE_FAILED = 1
EXIT_DATASET_INVALID = 2
EXIT_INTERNAL_ERROR = 3


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_config(path: Path) -> dict[str, object]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DatasetValidationError("evaluation configuration is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
        raise DatasetValidationError("evaluation configuration requires a version")
    return cast(dict[str, object], payload)


def _filter_cases[CaseT: EvaluationCaseBase](
    cases: tuple[CaseT, ...], *, case_id: str | None, tag: str | None
) -> tuple[CaseT, ...]:
    selected = tuple(
        case
        for case in cases
        if (case_id is None or case.case_id == case_id)
        and (tag is None or tag in case.tags)
    )
    if not selected:
        raise DatasetValidationError("case/tag filter selected no evaluation cases")
    return selected


async def _evaluate_cases(
    cases: tuple[Any, ...],
    *,
    dataset: Literal["plan", "memory"],
    ablation: AblationMode,
    config: dict[str, object],
) -> tuple[EvaluationCaseResult, ...]:
    raw_repetitions = config.get("deterministic_repetitions", 3)
    raw_timeout = config.get("case_timeout_seconds", 10)
    if not isinstance(raw_repetitions, int) or not isinstance(
        raw_timeout, (int, float)
    ):
        raise DatasetValidationError("evaluation repetition/timeout config is invalid")
    repetitions = raw_repetitions
    timeout = float(raw_timeout)
    evaluator: Any = (
        PlanCaseEvaluator(repetitions=repetitions, timeout_seconds=timeout)
        if dataset == "plan"
        else MemoryCaseEvaluator(repetitions=repetitions, timeout_seconds=timeout)
    )
    observability = build_observability(
        enabled=True,
        tracing_enabled=True,
        exporter_name="in_memory",
        service_name="fitweek-evaluation",
        metrics_enabled=True,
        structured_logging_enabled=False,
    )
    results: list[EvaluationCaseResult] = []
    try:
        for case in cases:
            context = ObservabilityContext(
                correlation_id=uuid5(
                    NAMESPACE_URL,
                    f"fitweek:evaluation:trace:{case.case_id}:{ablation.value}",
                ),
                operation_name="evaluation.case",
                component="evaluation",
            )
            with observability.start_span(
                "evaluation.case",
                context=context,
                attributes={
                    "dataset": dataset,
                    "category": case.category,
                    "case_id": case.case_id,
                    "ablation": ablation.value,
                },
            ) as span:
                result = await evaluator.evaluate(case, ablation)
                span.set_attributes(
                    {
                        "case_status": result.status.value,
                        "failure_category": result.failure_category.value
                        if result.failure_category
                        else "NONE",
                    }
                )
                if result.status is EvaluationCaseStatus.PASS:
                    span.succeed()
                else:
                    span.fail(
                        result.failure_category.value
                        if result.failure_category
                        else "EVALUATION_FAILED",
                        result.error_code,
                    )
            failure = (
                result.failure_category.value if result.failure_category else "NONE"
            )
            labels = {
                "dataset": dataset,
                "category": result.category,
                "outcome": result.status.value,
                "failure_category": failure,
                "ablation": ablation.value,
            }
            observability.record_counter(
                "fitweek_evaluation_cases_total", labels=labels
            )
            observability.record_histogram(
                "fitweek_evaluation_duration_seconds",
                result.duration_ms / 1000,
                labels={
                    "dataset": dataset,
                    "category": result.category,
                    "outcome": result.status.value,
                    "ablation": ablation.value,
                },
            )
            if result.status is not EvaluationCaseStatus.PASS:
                observability.record_counter(
                    "fitweek_evaluation_failures_total",
                    labels={
                        "dataset": dataset,
                        "category": result.category,
                        "failure_category": failure,
                        "ablation": ablation.value,
                    },
                )
            results.append(result)
    finally:
        observability.shutdown()
    return tuple(results)


def _report(
    *,
    dataset: Literal["plan", "memory"],
    results: tuple[EvaluationCaseResult, ...],
    metrics: dict[str, float],
    dataset_path: Path,
    config_path: Path,
    config: dict[str, object],
    gates: tuple[GateCheck, ...],
    started_at: datetime,
    ablation: AblationMode,
) -> EvaluationRunReport:
    completed = datetime.now(UTC)
    raw_seed = config.get("seed", 20260722)
    if not isinstance(raw_seed, int):
        raise DatasetValidationError("evaluation seed must be an integer")
    return EvaluationRunReport(
        dataset=dataset,
        dataset_version="phase-9a-benchmark-v1",
        config_version=str(config["version"]),
        schema_version="phase-9a-schema-v1",
        seed=raw_seed,
        timezone=str(config.get("timezone", "UTC")),
        ablation=ablation,
        started_at=started_at,
        completed_at=completed,
        dataset_sha256=file_sha256(dataset_path),
        config_sha256=file_sha256(config_path),
        total=len(results),
        passed=sum(item.status is EvaluationCaseStatus.PASS for item in results),
        failed=sum(item.status is EvaluationCaseStatus.FAIL for item in results),
        skipped=0,
        timed_out=sum(
            item.status is EvaluationCaseStatus.TIMED_OUT for item in results
        ),
        internal_error=sum(
            item.status is EvaluationCaseStatus.INTERNAL_ERROR for item in results
        ),
        metrics=metrics,
        gates=gates,
        gate_passed=all_gates_pass(gates),
        results=results,
    )


async def _run_standard(
    args: argparse.Namespace,
    root: Path,
    config: dict[str, object],
    gates_config: dict[str, object],
) -> int:
    dataset_path = root / "evaluation" / "datasets" / f"{args.dataset}_cases.jsonl"
    cases = (
        load_plan_cases(dataset_path)
        if args.dataset == "plan"
        else load_memory_cases(dataset_path)
    )
    selected = _filter_cases(cases, case_id=args.case_id, tag=args.tag)
    original_hash = file_sha256(dataset_path)
    started = datetime.now(UTC)
    results = await _evaluate_cases(
        selected, dataset=args.dataset, ablation=AblationMode.FULL, config=config
    )
    if file_sha256(dataset_path) != original_hash:
        raise DatasetValidationError("runner modified its dataset")
    metrics = (
        plan_metrics(results) if args.dataset == "plan" else memory_metrics(results)
    )
    formal = args.case_id is None and args.tag is None
    checks: tuple[GateCheck, ...] = ()
    if formal:
        checks = evaluate_gates(
            section=args.dataset, metrics=metrics, configuration=gates_config
        )
        checks += evaluate_gates(
            section="framework", metrics=metrics, configuration=gates_config
        )
    report = _report(
        dataset=args.dataset,
        results=results,
        metrics=metrics,
        dataset_path=dataset_path,
        config_path=Path(args.config),
        config=config,
        gates=checks,
        started_at=started,
        ablation=AblationMode.FULL,
    )
    output = (
        Path(args.output)
        if args.output
        else root / "evaluation" / "reports" / f"phase-9a-{args.dataset}-results.json"
    )
    output_hash = write_json(output, report)
    print(
        json.dumps(
            {
                "dataset": args.dataset,
                "total": report.total,
                "passed": report.passed,
                "failed": report.failed,
                "skipped": report.skipped,
                "timed_out": report.timed_out,
                "internal_error": report.internal_error,
                "gate_passed": report.gate_passed,
                "output": str(output),
                "output_sha256": output_hash,
            },
            sort_keys=True,
        )
    )
    return (
        EXIT_PASSED if report.gate_passed and report.failed == 0 else EXIT_GATE_FAILED
    )


async def _run_ablations(
    args: argparse.Namespace,
    root: Path,
    config: dict[str, object],
    gates_config: dict[str, object],
) -> int:
    plan_path = root / "evaluation" / "datasets" / "plan_cases.jsonl"
    memory_path = root / "evaluation" / "datasets" / "memory_cases.jsonl"
    plan_cases = load_plan_cases(plan_path)
    memory_cases = load_memory_cases(memory_path)
    specs = load_ablations(root / "evaluation" / "configs" / "ablations.yaml")
    comparisons: list[AblationComparison] = []
    all_safe = True
    for spec in specs:
        if not ablation_is_safe(spec):
            raise DatasetValidationError(
                "ablation attempts to disable a protected gate"
            )
        plan_results = await _evaluate_cases(
            plan_cases, dataset="plan", ablation=spec.mode, config=config
        )
        memory_results = await _evaluate_cases(
            memory_cases, dataset="memory", ablation=spec.mode, config=config
        )
        plan_values = plan_metrics(plan_results)
        memory_values = memory_metrics(memory_results)
        zero_values = (
            plan_values["hard_constraint_escape_count"]
            + plan_values["safety_escape_count"]
            + plan_values["calendar_side_effect_before_confirmation_count"]
            + plan_values["duplicate_calendar_event_count"]
            + plan_values["recovery_immutable_violation_count"]
            + memory_values["cross_user_memory_leak_count"]
            + memory_values["pending_memory_leak_count"]
            + memory_values["expired_memory_recall_count"]
            + memory_values["deleted_memory_recall_count"]
        )
        safe = zero_values == 0
        all_safe = all_safe and safe
        durations = [item.duration_ms for item in (*plan_results, *memory_results)]
        comparisons.append(
            AblationComparison(
                mode=spec.mode,
                plan_metrics=plan_values,
                memory_metrics=memory_values,
                average_evaluation_latency_ms=sum(durations) / len(durations),
                zero_tolerance_passed=safe,
            )
        )
    payload = {
        "evaluation": "phase-9a-controlled-ablations",
        "dataset_version": "phase-9a-benchmark-v1",
        "config_version": config["version"],
        "seed": config.get("seed", 20260722),
        "timezone": config.get("timezone", "UTC"),
        "comparisons": [item.model_dump(mode="json") for item in comparisons],
        "zero_tolerance_passed": all_safe,
    }
    output = (
        Path(args.output)
        if args.output
        else root / "evaluation" / "reports" / "phase-9a-ablation-results.json"
    )
    output_hash = write_json(output, payload)
    summary = {
        "phase": "PHASE 9A",
        "dataset_version": "phase-9a-benchmark-v1",
        "plan_result": "evaluation/reports/phase-9a-plan-results.json",
        "memory_result": "evaluation/reports/phase-9a-memory-results.json",
        "ablation_result": "evaluation/reports/phase-9a-ablation-results.json",
        "ablation_modes": [item.mode.value for item in comparisons],
        "zero_tolerance_passed": all_safe,
    }
    summary_path = root / "evaluation" / "reports" / "phase-9a-summary.json"
    summary_hash = write_json(summary_path, summary)
    print(
        json.dumps(
            {
                "dataset": "ablation",
                "modes": len(comparisons),
                "zero_tolerance_passed": all_safe,
                "output": str(output),
                "output_sha256": output_hash,
                "summary_sha256": summary_hash,
            },
            sort_keys=True,
        )
    )
    return EXIT_PASSED if all_safe else EXIT_GATE_FAILED


async def async_main(argv: list[str] | None = None) -> int:
    root = _repository_root()
    parser = argparse.ArgumentParser(description="FitWeek offline evaluation runner")
    parser.add_argument(
        "--dataset", choices=("plan", "memory", "ablation"), required=True
    )
    parser.add_argument(
        "--config",
        default=str(root / "evaluation" / "configs" / "phase_9a_default.yaml"),
    )
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--case-id", default=None)
    parser.add_argument("--tag", default=None)
    args = parser.parse_args(argv)
    try:
        verify_manifest(
            root / "evaluation" / "datasets" / "dataset_manifest.json", root
        )
        config = _load_config(Path(args.config))
        if args.seed is not None:
            config = {**config, "seed": args.seed}
        gates_config = load_gates(root / "evaluation" / "configs" / "gates.yaml")
        if args.dataset == "ablation":
            if args.case_id or args.tag:
                raise DatasetValidationError("ablation runs do not accept case filters")
            return await _run_ablations(args, root, config, gates_config)
        return await _run_standard(args, root, config, gates_config)
    except DatasetValidationError as exc:
        print(json.dumps({"error_code": "DATASET_INVALID", "safe_detail": str(exc)}))
        return EXIT_DATASET_INVALID
    except Exception:
        print(json.dumps({"error_code": "EVALUATION_FRAMEWORK_INTERNAL_FAILURE"}))
        return EXIT_INTERNAL_ERROR


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
