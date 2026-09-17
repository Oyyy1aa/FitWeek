"""Dataset schema, distribution, integrity, and privacy gates."""

import json
import re
from collections import Counter

import pytest

from app.evaluation.dataset_generator import (
    generate_memory_cases,
    generate_plan_cases,
    render_jsonl,
)
from app.evaluation.dataset_loader import (
    MEMORY_CATEGORY_MINIMUMS,
    PLAN_CATEGORY_MINIMUMS,
    DatasetValidationError,
    file_sha256,
    load_manifest,
    load_plan_cases,
    validate_cases,
    verify_manifest,
)
from app.evaluation.models import MemoryEvaluationCase, PlanEvaluationCase
from app.evaluation.redaction import find_forbidden_paths

pytestmark = pytest.mark.phase_9a


def test_plan_dataset_contains_exactly_200_cases(plan_cases) -> None:
    assert len(plan_cases) == 200


def test_memory_dataset_contains_108_cases(memory_cases) -> None:
    assert len(memory_cases) == 108


@pytest.mark.parametrize("category,minimum", PLAN_CATEGORY_MINIMUMS.items())
def test_plan_category_distribution(plan_cases, category: str, minimum: int) -> None:
    assert Counter(item.category for item in plan_cases)[category] >= minimum


@pytest.mark.parametrize("category,minimum", MEMORY_CATEGORY_MINIMUMS.items())
def test_memory_category_distribution(
    memory_cases, category: str, minimum: int
) -> None:
    assert Counter(item.category for item in memory_cases)[category] >= minimum


@pytest.mark.parametrize(
    "fixture,prefix,expected_count",
    [("plan_cases", "PLAN", 200), ("memory_cases", "MEM", 108)],
)
def test_case_ids_are_unique_and_stable(
    request, fixture: str, prefix: str, expected_count: int
) -> None:
    cases = request.getfixturevalue(fixture)
    identifiers = [item.case_id for item in cases]
    assert len(identifiers) == len(set(identifiers)) == expected_count
    assert all(re.fullmatch(rf"{prefix}-[0-9]{{3}}", item) for item in identifiers)


@pytest.mark.parametrize("fixture", ["plan_cases", "memory_cases"])
def test_every_case_has_seed_timezone_versions_and_invariants(request, fixture) -> None:
    cases = request.getfixturevalue(fixture)
    assert all(item.seed > 0 and item.timezone == "UTC" for item in cases)
    assert all(item.dataset_version == "phase-9a-benchmark-v1" for item in cases)
    assert all(item.schema_version == "phase-9a-schema-v1" for item in cases)
    assert all(item.expected_invariants and item.tags for item in cases)


def test_committed_plan_fixture_matches_generator(plan_cases) -> None:
    assert render_jsonl(generate_plan_cases()) == render_jsonl(plan_cases)


def test_committed_memory_fixture_matches_generator(memory_cases) -> None:
    assert render_jsonl(generate_memory_cases()) == render_jsonl(memory_cases)


def test_dataset_manifest_hashes_and_counts(repository_root) -> None:
    manifest = verify_manifest(
        repository_root / "evaluation/datasets/dataset_manifest.json", repository_root
    )
    assert {item.dataset_name: item.case_count for item in manifest.datasets} == {
        "plan": 200,
        "memory": 108,
    }


def test_dataset_manifest_is_schema_valid(repository_root) -> None:
    manifest = load_manifest(
        repository_root / "evaluation/datasets/dataset_manifest.json"
    )
    assert manifest.manifest_version == "phase-9a-dataset-manifest-v1"
    assert all(len(item.sha256) == 64 for item in manifest.datasets)


@pytest.mark.parametrize(
    "schema_file,model",
    [
        ("plan_case_schema.json", PlanEvaluationCase),
        ("memory_case_schema.json", MemoryEvaluationCase),
    ],
)
def test_committed_json_schema_matches_model(
    repository_root, schema_file, model
) -> None:
    actual = json.loads(
        (repository_root / "evaluation/datasets" / schema_file).read_text(
            encoding="utf-8"
        )
    )
    assert actual == model.model_json_schema()


@pytest.mark.parametrize("fixture", ["plan_cases", "memory_cases"])
def test_dataset_has_no_forbidden_fields_or_paths(request, fixture) -> None:
    cases = request.getfixturevalue(fixture)
    assert all(not find_forbidden_paths(item.model_dump(mode="json")) for item in cases)


def test_duplicate_case_id_is_rejected(plan_cases) -> None:
    with pytest.raises(DatasetValidationError, match="IDs"):
        validate_cases(
            (plan_cases[0], plan_cases[0], *plan_cases[2:]),
            minimum_count=200,
            category_minimums=PLAN_CATEGORY_MINIMUMS,
        )


def test_truncated_dataset_is_rejected(tmp_path, repository_root) -> None:
    source = repository_root / "evaluation/datasets/plan_cases.jsonl"
    target = tmp_path / "plan.jsonl"
    target.write_text("\n".join(source.read_text(encoding="utf-8").splitlines()[:10]))
    with pytest.raises(DatasetValidationError, match="count"):
        load_plan_cases(target)


def test_dataset_hash_changes_when_fixture_changes(tmp_path, repository_root) -> None:
    source = repository_root / "evaluation/datasets/memory_cases.jsonl"
    target = tmp_path / "memory.jsonl"
    target.write_bytes(source.read_bytes() + b"\n")
    assert file_sha256(target) != file_sha256(source)
