"""Strict JSONL loading and dataset integrity validation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from app.evaluation.determinism import stable_fingerprint
from app.evaluation.models import (
    DatasetManifest,
    EvaluationCaseBase,
    MemoryEvaluationCase,
    PlanEvaluationCase,
)
from app.evaluation.redaction import find_forbidden_paths

PLAN_CATEGORY_MINIMUMS = {
    "basic_profile": 20,
    "weekly_frequency": 15,
    "session_duration": 15,
    "equipment_constraint": 15,
    "location_constraint": 15,
    "excluded_feature": 15,
    "multi_constraint": 20,
    "no_available_exercise": 10,
    "schedule_time_window": 15,
    "calendar_busy_manual_only": 10,
    "recovery_spacing": 15,
    "completed_checked_in_immutable": 10,
    "plan_version_cas_idempotency": 10,
    "boundary_invalid": 15,
}

MEMORY_CATEGORY_MINIMUMS = {
    "active_recall": 15,
    "pending_review_not_recalled": 10,
    "rejected_not_recalled": 8,
    "deleted_not_recalled": 8,
    "expired_not_recalled": 8,
    "ttl_boundary": 8,
    "duplicate_candidate": 8,
    "conflict_candidate": 8,
    "evidence_validation": 8,
    "no_memory_degradation": 8,
    "user_isolation": 10,
    "context_ordering": 9,
}


class DatasetValidationError(ValueError):
    """Raised before evaluation when a benchmark is not trustworthy."""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_jsonl[CaseT: EvaluationCaseBase](
    path: Path, model: type[CaseT]
) -> tuple[CaseT, ...]:
    cases: list[CaseT] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetValidationError("dataset could not be read") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise DatasetValidationError(f"blank JSONL line at {line_number}")
        try:
            payload = json.loads(line)
            cases.append(model.model_validate(payload))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise DatasetValidationError(
                f"dataset line {line_number} violates its schema"
            ) from exc
    return tuple(cases)


def load_plan_cases(path: Path) -> tuple[PlanEvaluationCase, ...]:
    cases = load_jsonl(path, PlanEvaluationCase)
    validate_cases(cases, minimum_count=200, category_minimums=PLAN_CATEGORY_MINIMUMS)
    return cases


def load_memory_cases(path: Path) -> tuple[MemoryEvaluationCase, ...]:
    cases = load_jsonl(path, MemoryEvaluationCase)
    validate_cases(cases, minimum_count=100, category_minimums=MEMORY_CATEGORY_MINIMUMS)
    return cases


def validate_cases[CaseT: EvaluationCaseBase](
    cases: tuple[CaseT, ...],
    *,
    minimum_count: int,
    category_minimums: dict[str, int],
) -> None:
    if len(cases) < minimum_count:
        raise DatasetValidationError("dataset case count is below the required gate")
    identifiers = [case.case_id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise DatasetValidationError("case IDs must be unique")
    distributions = Counter(case.category for case in cases)
    for category, minimum in category_minimums.items():
        if distributions[category] < minimum:
            raise DatasetValidationError(
                f"category {category} is below its required distribution"
            )
    fingerprints = [
        stable_fingerprint(case.model_dump(mode="json", exclude={"case_id"}))
        for case in cases
    ]
    if len(fingerprints) != len(set(fingerprints)):
        raise DatasetValidationError("duplicate case content is not allowed")
    for case in cases:
        findings = find_forbidden_paths(case.model_dump(mode="json"))
        if findings:
            raise DatasetValidationError("dataset contains a forbidden field or path")


def load_manifest(path: Path) -> DatasetManifest:
    try:
        return DatasetManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise DatasetValidationError("dataset manifest is invalid") from exc


def verify_manifest(manifest_path: Path, repository_root: Path) -> DatasetManifest:
    manifest = load_manifest(manifest_path)
    names: set[str] = set()
    for entry in manifest.datasets:
        if entry.dataset_name in names:
            raise DatasetValidationError("dataset manifest contains duplicate names")
        names.add(entry.dataset_name)
        relative = Path(entry.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise DatasetValidationError("dataset path must be repository-relative")
        path = repository_root / relative
        if file_sha256(path) != entry.sha256:
            raise DatasetValidationError("dataset hash does not match its manifest")
        model = (
            PlanEvaluationCase if entry.dataset_name == "plan" else MemoryEvaluationCase
        )
        cases = load_jsonl(path, model)
        if len(cases) != entry.case_count:
            raise DatasetValidationError("dataset count does not match its manifest")
    if names != {"plan", "memory"}:
        raise DatasetValidationError("manifest must describe plan and memory datasets")
    return manifest
