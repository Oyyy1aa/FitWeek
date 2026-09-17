"""Shared Phase 9A fixtures use only committed synthetic datasets."""

from pathlib import Path

import pytest

from app.evaluation.dataset_loader import load_memory_cases, load_plan_cases


@pytest.fixture(scope="session")
def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def plan_cases(repository_root: Path):
    return load_plan_cases(repository_root / "evaluation/datasets/plan_cases.jsonl")


@pytest.fixture(scope="session")
def memory_cases(repository_root: Path):
    return load_memory_cases(repository_root / "evaluation/datasets/memory_cases.jsonl")
