"""Phase 8B2 asset fixtures."""

from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def recording_rules(project_root: Path) -> dict[str, object]:
    value = yaml.safe_load(
        (project_root / "observability/prometheus/recording-rules.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(value, dict)
    return value


@pytest.fixture(scope="session")
def alert_rules(project_root: Path) -> dict[str, object]:
    value = yaml.safe_load(
        (project_root / "observability/prometheus/alert-rules.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(value, dict)
    return value
