"""Strict static validation for versioned Grafana, Prometheus, and routing assets."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from app.observability.metrics import DEFAULT_METRIC_DESCRIPTORS

METRIC_REFERENCE = re.compile(
    r"\b(?:fitweek(?::|_)[a-zA-Z0-9_:]+|http_server_[a-zA-Z0-9_:]+)\b"
)
FORBIDDEN_ASSET_TERMS = frozenset(
    {
        "user_id",
        "request_id",
        "correlation_id",
        "run_id",
        "step_id",
        "plan_id",
        "session_id",
        "memory_id",
        "calendar_id",
        "external_event_id",
        "operation_key",
        "authorization",
        "api_key",
        "password",
        "private_key",
    }
)


@dataclass(frozen=True, slots=True)
class AssetValidationSummary:
    dashboard_definitions_valid: bool
    recording_rules_valid: bool
    alert_rules_valid: bool
    routing_policy_valid: bool
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors


class AlertingAssetValidator:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.base_metrics = {item.name for item in DEFAULT_METRIC_DESCRIPTORS}

    def validate(self) -> AssetValidationSummary:
        errors: list[str] = []
        dashboards = self._validate_dashboards(errors)
        records, recording = self._validate_recording_rules(errors)
        alerts = self._validate_alert_rules(errors, records)
        routing = self._validate_routing(errors)
        return AssetValidationSummary(
            dashboards, recording, alerts, routing, tuple(errors)
        )

    def _load_yaml(self, path: Path, errors: list[str]) -> dict[str, Any]:
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{path.name}:YAML:{type(exc).__name__}")
            return {}
        return value if isinstance(value, dict) else {}

    def _validate_dashboards(self, errors: list[str]) -> bool:
        folder = self.project_root / "observability" / "grafana" / "dashboards"
        paths = sorted(folder.glob("*.json"))
        before = len(errors)
        if len(paths) != 5:
            errors.append("dashboards:expected-five")
        uids: set[str] = set()
        allowed_metrics = self.base_metrics | self._record_names()
        for path in paths:
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(f"{path.name}:JSON:{type(exc).__name__}")
                continue
            uid = str(doc.get("uid", ""))
            if not uid or uid in uids:
                errors.append(f"{path.name}:uid")
            uids.add(uid)
            if (
                not doc.get("title")
                or not doc.get("description")
                or not doc.get("schemaVersion")
            ):
                errors.append(f"{path.name}:metadata")
            panels = doc.get("panels", [])
            ids = [panel.get("id") for panel in panels if isinstance(panel, dict)]
            if not panels or len(ids) != len(set(ids)):
                errors.append(f"{path.name}:panels")
            if not any(panel.get("type") == "timeseries" for panel in panels):
                errors.append(f"{path.name}:timeseries")
            raw = json.dumps(doc, ensure_ascii=False).lower()
            if any(term in raw for term in FORBIDDEN_ASSET_TERMS):
                errors.append(f"{path.name}:forbidden")
            if "http://" in raw or "https://" in raw:
                errors.append(f"{path.name}:url")
            for panel in panels:
                for target in panel.get("targets", []):
                    expr = str(target.get("expr", "")).strip()
                    if not expr:
                        errors.append(f"{path.name}:empty-expression")
                    for metric in METRIC_REFERENCE.findall(expr):
                        normalized = metric.removesuffix("_bucket")
                        if normalized not in allowed_metrics:
                            errors.append(f"{path.name}:unknown:{metric}")
        return len(errors) == before

    def _record_names(self) -> set[str]:
        path = (
            self.project_root / "observability" / "prometheus" / "recording-rules.yaml"
        )
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        return {
            str(rule["record"])
            for group in doc.get("groups", [])
            for rule in group.get("rules", [])
            if isinstance(rule, dict) and "record" in rule
        }

    def _validate_recording_rules(self, errors: list[str]) -> tuple[set[str], bool]:
        path = (
            self.project_root / "observability" / "prometheus" / "recording-rules.yaml"
        )
        doc = self._load_yaml(path, errors)
        before = len(errors)
        groups = doc.get("groups", [])
        group_names = [group.get("name") for group in groups]
        if not groups or len(group_names) != len(set(group_names)):
            errors.append("recording-rules:groups")
        records: set[str] = set()
        for group in groups:
            for rule in group.get("rules", []):
                name = str(rule.get("record", ""))
                expr = str(rule.get("expr", "")).strip()
                if not name or name in records or not expr:
                    errors.append("recording-rules:record")
                records.add(name)
                if "/" in expr and "clamp_min" not in expr:
                    errors.append(f"recording-rules:zero-denominator:{name}")
                for metric in METRIC_REFERENCE.findall(expr):
                    normalized = metric.removesuffix("_bucket")
                    if (
                        normalized not in self.base_metrics
                        and normalized not in records
                    ):
                        errors.append(f"recording-rules:unknown:{metric}")
        return records, len(errors) == before

    def _validate_alert_rules(self, errors: list[str], records: set[str]) -> bool:
        path = self.project_root / "observability" / "prometheus" / "alert-rules.yaml"
        doc = self._load_yaml(path, errors)
        before = len(errors)
        names: set[str] = set()
        for group in doc.get("groups", []):
            for rule in group.get("rules", []):
                name = str(rule.get("alert", ""))
                expr = str(rule.get("expr", "")).strip()
                labels = rule.get("labels", {})
                annotations = rule.get("annotations", {})
                if not name or name in names or not expr:
                    errors.append("alert-rules:name")
                names.add(name)
                if labels.get("severity") not in {
                    "warning",
                    "critical",
                } or not labels.get("component"):
                    errors.append(f"alert-rules:labels:{name}")
                if (
                    not annotations.get("summary")
                    or not annotations.get("description")
                    or not annotations.get("runbook_url")
                ):
                    errors.append(f"alert-rules:annotations:{name}")
                if not re.fullmatch(r"\d+(?:s|m|h)", str(rule.get("for", ""))):
                    errors.append(f"alert-rules:for:{name}")
                runbook_target = str(annotations.get("runbook_url", ""))
                runbook = self.project_root / runbook_target.split("#", maxsplit=1)[0]
                if not runbook.is_file():
                    errors.append(f"alert-rules:runbook:{name}")
                for metric in METRIC_REFERENCE.findall(expr):
                    normalized = metric.removesuffix("_bucket")
                    if (
                        normalized not in self.base_metrics
                        and normalized not in records
                    ):
                        errors.append(f"alert-rules:unknown:{metric}")
        return len(errors) == before

    def _validate_routing(self, errors: list[str]) -> bool:
        path = self.project_root / "observability" / "alerting" / "routing-policy.yaml"
        doc = self._load_yaml(path, errors)
        before = len(errors)
        required = {
            "default",
            "warning",
            "critical",
            "security",
            "calendar",
            "observability",
        }
        receivers = {str(item.get("name")) for item in doc.get("receivers", [])}
        if receivers != required:
            errors.append("routing:receivers")
        for receiver in doc.get("receivers", []):
            if receiver.get("type") not in {"in_memory", "file_test_sink", "disabled"}:
                errors.append("routing:receiver-type")
        return len(errors) == before
