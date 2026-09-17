"""Static Grafana JSON validation against the exported metric contract."""

import json
from pathlib import Path

import pytest

from app.alerting.validation import FORBIDDEN_ASSET_TERMS, AlertingAssetValidator

pytestmark = pytest.mark.phase_8b2

DASHBOARDS = (
    "fitweek-overview.json",
    "fitweek-agents-models.json",
    "fitweek-tools-calendar.json",
    "fitweek-memory-recovery.json",
    "fitweek-orchestrator.json",
)


def _documents(root: Path) -> list[dict[str, object]]:
    folder = root / "observability/grafana/dashboards"
    return [
        json.loads((folder / name).read_text(encoding="utf-8")) for name in DASHBOARDS
    ]


@pytest.mark.parametrize("name", DASHBOARDS)
def test_each_dashboard_is_valid_json_with_stable_metadata(
    project_root: Path, name: str
) -> None:
    doc = json.loads(
        (project_root / "observability/grafana/dashboards" / name).read_text(
            encoding="utf-8"
        )
    )
    assert doc["uid"].startswith("fitweek-")
    assert doc["title"] and doc["description"]
    assert doc["schemaVersion"] >= 39
    assert doc["time"] == {"from": "now-6h", "to": "now"}


def test_dashboard_uids_are_unique(project_root: Path) -> None:
    uids = [doc["uid"] for doc in _documents(project_root)]
    assert len(uids) == len(set(uids)) == 5


def test_panel_ids_are_unique_within_each_dashboard(project_root: Path) -> None:
    for doc in _documents(project_root):
        ids = [panel["id"] for panel in doc["panels"]]
        assert len(ids) == len(set(ids))


def test_every_panel_has_a_nonempty_expression(project_root: Path) -> None:
    for doc in _documents(project_root):
        for panel in doc["panels"]:
            assert all(target["expr"].strip() for target in panel["targets"])


def test_every_panel_uses_prometheus_datasource(project_root: Path) -> None:
    for doc in _documents(project_root):
        for panel in doc["panels"]:
            assert panel["datasource"]["uid"] == "prometheus"


def test_every_dashboard_has_a_time_series_panel(project_root: Path) -> None:
    for doc in _documents(project_root):
        assert any(panel["type"] == "timeseries" for panel in doc["panels"])


def test_panels_define_units_and_no_data_text(project_root: Path) -> None:
    for doc in _documents(project_root):
        for panel in doc["panels"]:
            defaults = panel["fieldConfig"]["defaults"]
            assert defaults["unit"]
            assert defaults["noValue"] == "No data"


def test_dashboard_variables_are_low_cardinality(project_root: Path) -> None:
    allowed = {
        "environment",
        "component",
        "operation",
        "outcome",
        "agent_type",
        "provider_name",
        "tool_id",
        "error_category",
        "degradation_mode",
        "workflow_type",
        "step_type",
    }
    for doc in _documents(project_root):
        assert {item["name"] for item in doc["templating"]["list"]} <= allowed


def test_dashboards_contain_no_forbidden_identifiers(project_root: Path) -> None:
    raw = json.dumps(_documents(project_root), ensure_ascii=False).casefold()
    assert not any(term in raw for term in FORBIDDEN_ASSET_TERMS)


def test_dashboards_contain_no_url_or_secret(project_root: Path) -> None:
    raw = json.dumps(_documents(project_root), ensure_ascii=False).casefold()
    assert "http://" not in raw and "https://" not in raw
    assert "bearer " not in raw and "secret" not in raw


def test_dashboard_metric_references_are_registered(project_root: Path) -> None:
    result = AlertingAssetValidator(project_root).validate()
    assert result.dashboard_definitions_valid
    assert not [item for item in result.errors if "unknown" in item]


def test_major_alerts_have_matching_dashboard_panels(project_root: Path) -> None:
    titles = {
        panel["title"] for doc in _documents(project_root) for panel in doc["panels"]
    }
    assert {
        "Agent success rate",
        "Calendar write failure rate",
        "Expired recall (must be zero)",
        "Exporter degradation",
        "Firing alerts",
        "Backlog",
    } <= titles
