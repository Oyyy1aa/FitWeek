"""Aggregate benchmark metrics without hiding zero-tolerance failures."""

from __future__ import annotations

from collections.abc import Iterable

from app.evaluation.models import EvaluationCaseResult, EvaluationCaseStatus


def _mean(values: Iterable[float]) -> float:
    materialized = tuple(values)
    return sum(materialized) / len(materialized) if materialized else 0.0


def _sum(results: tuple[EvaluationCaseResult, ...], key: str) -> float:
    return sum(item.metric_values.get(key, 0.0) for item in results)


def plan_metrics(results: tuple[EvaluationCaseResult, ...]) -> dict[str, float]:
    total = len(results)
    passed = sum(item.status is EvaluationCaseStatus.PASS for item in results)
    return {
        "total_cases": float(total),
        "plan_case_pass_rate": passed / total if total else 0.0,
        "plan_generation_success_rate": _mean(
            item.metric_values.get("plan_generation_success", 0.0) for item in results
        ),
        "hard_constraint_escape_count": _sum(results, "hard_constraint_escape"),
        "safety_escape_count": _sum(results, "safety_escape"),
        "safety_rejection_accuracy": _mean(
            item.metric_values.get("safety_rejection_accurate", 0.0)
            for item in results
            if item.error_code is not None
        ),
        "deterministic_output_rate": _mean(
            item.metric_values.get("deterministic_output", 0.0) for item in results
        ),
        "plan_fingerprint_stability_rate": _mean(
            item.metric_values.get("plan_fingerprint_stable", 0.0) for item in results
        ),
        "schedule_conflict_detection_rate": _mean(
            item.metric_values.get("schedule_conflict_detected", 0.0)
            for item in results
            if item.category == "schedule_time_window"
        ),
        "calendar_side_effect_before_confirmation_count": _sum(
            results, "calendar_side_effect_before_confirmation"
        ),
        "duplicate_calendar_event_count": _sum(results, "duplicate_calendar_event"),
        "recovery_immutable_violation_count": _sum(
            results, "recovery_immutable_violation"
        ),
        "fallback_rate": _mean(
            item.metric_values.get("fallback_used", 0.0) for item in results
        ),
        "manual_only_rate": _mean(
            item.metric_values.get("manual_only", 0.0) for item in results
        ),
        "average_session_count": _mean(
            item.metric_values.get("session_count", 0.0) for item in results
        ),
        "average_plan_modifications": _mean(
            item.metric_values.get("plan_modifications", 0.0) for item in results
        ),
        **framework_metrics(results),
    }


def memory_metrics(results: tuple[EvaluationCaseResult, ...]) -> dict[str, float]:
    total = len(results)
    passed = sum(item.status is EvaluationCaseStatus.PASS for item in results)
    return {
        "total_cases": float(total),
        "memory_case_pass_rate": passed / total if total else 0.0,
        "active_memory_recall_accuracy": _mean(
            item.metric_values.get("active_memory_recall_accurate", 0.0)
            for item in results
            if item.category in {"active_recall", "evidence_validation"}
        ),
        "pending_memory_leak_count": _sum(results, "pending_memory_leak"),
        "expired_memory_recall_count": _sum(results, "expired_memory_recall"),
        "deleted_memory_recall_count": _sum(results, "deleted_memory_recall"),
        "cross_user_memory_leak_count": _sum(results, "cross_user_memory_leak"),
        "memory_candidate_idempotency_rate": _mean(
            item.metric_values.get("memory_candidate_idempotent", 0.0)
            for item in results
            if item.category == "duplicate_candidate"
        ),
        "memory_conflict_detection_rate": _mean(
            item.metric_values.get("memory_conflict_detected", 0.0)
            for item in results
            if item.category == "conflict_candidate"
        ),
        "no_memory_degradation_success_rate": _mean(
            item.metric_values.get("no_memory_degradation_succeeded", 0.0)
            for item in results
            if item.category == "no_memory_degradation"
        ),
        "context_determinism_rate": _mean(
            item.metric_values.get("context_deterministic", 0.0) for item in results
        ),
        **framework_metrics(results),
    }


def framework_metrics(results: tuple[EvaluationCaseResult, ...]) -> dict[str, float]:
    return {
        "evaluation_internal_error_count": float(
            sum(item.status is EvaluationCaseStatus.INTERNAL_ERROR for item in results)
        ),
        "case_timeout_count": float(
            sum(item.status is EvaluationCaseStatus.TIMED_OUT for item in results)
        ),
        "case_retry_count": 0.0,
        "unexpected_side_effect_count": _sum(results, "unexpected_side_effect"),
        "non_deterministic_case_count": float(
            sum("NON_DETERMINISTIC_OUTPUT" in item.violations for item in results)
        ),
    }
