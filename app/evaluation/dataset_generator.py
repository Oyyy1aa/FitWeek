"""Reproducible generator for the committed synthetic benchmark fixtures."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable

from app.evaluation.models import (
    MemoryCaseInput,
    MemoryEvaluationCase,
    PlanCaseInput,
    PlanConstraintFixture,
    PlanEvaluationCase,
)

DATASET_VERSION = "phase-9a-benchmark-v1"
SCHEMA_VERSION = "phase-9a-schema-v1"
GENERATOR_VERSION = "phase-9a-generator-v1"
DEFAULT_SEED = 20260722
DEFAULT_TIMEZONE = "UTC"

PLAN_DISTRIBUTION = (
    ("basic_profile", 20),
    ("weekly_frequency", 15),
    ("session_duration", 15),
    ("equipment_constraint", 15),
    ("location_constraint", 15),
    ("excluded_feature", 15),
    ("multi_constraint", 20),
    ("no_available_exercise", 10),
    ("schedule_time_window", 15),
    ("calendar_busy_manual_only", 10),
    ("recovery_spacing", 15),
    ("completed_checked_in_immutable", 10),
    ("plan_version_cas_idempotency", 10),
    ("boundary_invalid", 15),
)

MEMORY_DISTRIBUTION = (
    ("active_recall", 15, "ACTIVE_RECALL", 1),
    ("pending_review_not_recalled", 10, "PENDING_REVIEW", 0),
    ("rejected_not_recalled", 8, "REJECTED", 0),
    ("deleted_not_recalled", 8, "DELETED", 0),
    ("expired_not_recalled", 8, "EXPIRED", 0),
    ("ttl_boundary", 8, "TTL_BOUNDARY", 0),
    ("duplicate_candidate", 8, "DUPLICATE_CANDIDATE", 0),
    ("conflict_candidate", 8, "CONFLICT_CANDIDATE", 0),
    ("evidence_validation", 8, "EVIDENCE", 1),
    ("no_memory_degradation", 8, "NO_MEMORY", 0),
    ("user_isolation", 10, "CROSS_USER", 0),
    ("context_ordering", 9, "CONTEXT_ORDER", 2),
)


def _plan_input(
    category: str, ordinal: int
) -> tuple[PlanCaseInput, str, tuple[str, ...]]:
    frequencies = (2, 3, 4, 5)
    durations = (15, 30, 45, 60)
    locations = ("HOME", "GYM", "OUTDOOR")
    goals = (
        "BUILD_HABIT",
        "GENERAL_FITNESS",
        "BASIC_STRENGTH",
        "LOW_IMPACT_CARDIO",
        "MOBILITY",
        "MIXED",
    )
    frequency = frequencies[ordinal % len(frequencies)]
    maximum = durations[ordinal % len(durations)]
    location = locations[ordinal % len(locations)]
    experience = "INTERMEDIATE" if ordinal % 3 == 0 else "BEGINNER"
    goal = goals[ordinal % len(goals)]
    constraints: tuple[PlanConstraintFixture, ...] = ()
    values: dict[str, object] = {}
    outcome = "SUCCEEDED"
    rejection: tuple[str, ...] = ()

    if category == "equipment_constraint":
        location = "GYM"
        experience = "INTERMEDIATE"
        goal = "BASIC_STRENGTH"
        equipment = ("dumbbell", "resistance_band", "yoga_mat")[ordinal % 3]
        constraints = (
            PlanConstraintFixture(
                constraint_type="AVAILABLE_EQUIPMENT", value_code=equipment
            ),
        )
    elif category == "location_constraint":
        constraints = (
            PlanConstraintFixture(
                constraint_type="ALLOWED_LOCATION", value_code=location
            ),
        )
    elif category == "excluded_feature":
        feature = ("floor_required", "jumping", "running")[ordinal % 3]
        constraints = (
            PlanConstraintFixture(
                constraint_type="EXCLUDED_FEATURE", value_code=feature
            ),
        )
    elif category == "multi_constraint":
        location = "HOME"
        maximum = (30, 45, 60)[ordinal % 3]
        constraints = (
            PlanConstraintFixture(
                constraint_type="ALLOWED_LOCATION", value_code="HOME"
            ),
            PlanConstraintFixture(
                constraint_type="AVAILABLE_EQUIPMENT", value_code="resistance_band"
            ),
            PlanConstraintFixture(
                constraint_type="EXCLUDED_FEATURE", value_code="floor_required"
            ),
            PlanConstraintFixture(
                constraint_type="MAX_SESSION_MINUTES", value_code=str(maximum)
            ),
        )
    elif category == "no_available_exercise":
        values["catalog_mode"] = "DISABLED_ONLY"
        outcome = "REJECTED"
        rejection = ("NO_ELIGIBLE_EXERCISES",)
    elif category == "schedule_time_window":
        if ordinal % 3 == 1:
            frequency = 5
            values["slot_count"] = 1
            outcome = "REJECTED"
            rejection = ("INSUFFICIENT_AVAILABILITY",)
        elif ordinal % 3 == 2:
            values["schedule_variant"] = "OVERLAP"
            outcome = "REJECTED"
            rejection = ("AVAILABILITY_SLOT_OVERLAP",)
    elif category == "calendar_busy_manual_only":
        values["calendar_mode"] = "MANUAL_ONLY"
    elif category == "completed_checked_in_immutable":
        values["history_mode"] = "COMPLETED" if ordinal % 2 == 0 else "CHECKED_IN"
    elif category == "plan_version_cas_idempotency":
        values["expected_plan_version"] = 1
    elif category == "boundary_invalid":
        mode = ordinal % 3
        if mode == 0:
            values["scope_confirmed"] = False
            outcome = "REJECTED"
            rejection = ("SCOPE_NOT_CONFIRMED",)
        elif mode == 1:
            maximum = 61
            outcome = "REJECTED"
            rejection = ("DOMAIN_VALIDATION_ERROR",)
        else:
            values["slot_duration_minutes"] = 10
            outcome = "REJECTED"
            rejection = ("NO_VALID_TIME_SLOT",)

    fixture = PlanCaseInput(
        weekly_frequency=frequency,
        max_session_minutes=maximum,
        experience_level=experience,
        primary_goal=goal,
        location=location,
        constraints=constraints,
        **values,
    )
    return fixture, outcome, rejection


def generate_plan_cases() -> tuple[PlanEvaluationCase, ...]:
    cases: list[PlanEvaluationCase] = []
    number = 1
    invariants = (
        "PROFILE_VALID",
        "CONSTRAINTS_APPLIED",
        "FREQUENCY_VALID",
        "DURATION_VALID",
        "CATALOG_ACTIVE_ONLY",
        "EQUIPMENT_MATCH",
        "LOCATION_MATCH",
        "EXCLUDED_FEATURE_ABSENT",
        "SAFETY_PASSED_OR_REJECTED",
        "NO_OVERLAP",
        "DETERMINISTIC",
        "NO_UNCONFIRMED_SIDE_EFFECT",
    )
    for category, count in PLAN_DISTRIBUTION:
        for ordinal in range(count):
            fixture, outcome, rejection = _plan_input(category, ordinal)
            cases.append(
                PlanEvaluationCase(
                    case_id=f"PLAN-{number:03d}",
                    dataset_version=DATASET_VERSION,
                    schema_version=SCHEMA_VERSION,
                    category=category,
                    description_code=f"{category.upper()}_{ordinal + 1:02d}",
                    seed=DEFAULT_SEED + number,
                    timezone=DEFAULT_TIMEZONE,
                    expected_outcome=outcome,
                    expected_invariants=invariants,
                    expected_rejection_codes=rejection,
                    tags=("plan", category, outcome.casefold()),
                    input=fixture,
                )
            )
            number += 1
    return tuple(cases)


def generate_memory_cases() -> tuple[MemoryEvaluationCase, ...]:
    cases: list[MemoryEvaluationCase] = []
    number = 1
    invariants = (
        "USER_ISOLATION",
        "STATUS_GATE",
        "TTL_GATE",
        "EVIDENCE_GATE",
        "ACCEPT_BEFORE_RECALL",
        "DELETED_EXPIRED_NOT_RECALLED",
        "CONTEXT_ORDER_STABLE",
        "NO_MEMORY_DEGRADES",
        "DETERMINISTIC",
    )
    for category, count, scenario, recall_count in MEMORY_DISTRIBUTION:
        for ordinal in range(count):
            offset = 0 if scenario == "TTL_BOUNDARY" else 86400 + ordinal
            cases.append(
                MemoryEvaluationCase(
                    case_id=f"MEM-{number:03d}",
                    dataset_version=DATASET_VERSION,
                    schema_version=SCHEMA_VERSION,
                    category=category,
                    description_code=f"{category.upper()}_{ordinal + 1:02d}",
                    seed=DEFAULT_SEED + 1000 + number,
                    timezone=DEFAULT_TIMEZONE,
                    expected_outcome="SUCCEEDED",
                    expected_invariants=invariants,
                    tags=("memory", category, "safety"),
                    input=MemoryCaseInput(
                        scenario=scenario,
                        valid_until_offset_seconds=offset,
                        expected_recall_count=recall_count,
                        query_failure_count=2 if scenario == "NO_MEMORY" else 0,
                    ),
                )
            )
            number += 1
    return tuple(cases)


def render_jsonl(cases: Iterable[PlanEvaluationCase | MemoryEvaluationCase]) -> str:
    return "\n".join(
        json.dumps(case.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        for case in cases
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=("plan", "memory"), required=True)
    parser.add_argument("--schema", action="store_true")
    args = parser.parse_args()
    if args.schema:
        model = PlanEvaluationCase if args.dataset == "plan" else MemoryEvaluationCase
        print(json.dumps(model.model_json_schema(), indent=2, sort_keys=True))
    else:
        cases = (
            generate_plan_cases() if args.dataset == "plan" else generate_memory_cases()
        )
        print(render_jsonl(cases))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
