"""Prometheus metrics with a strict low-cardinality descriptor gate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

ALLOWED_METRIC_LABELS = frozenset(
    {
        "component",
        "operation",
        "agent_type",
        "tool_id",
        "tool_version",
        "provider_name",
        "outcome",
        "error_category",
        "degradation_mode",
        "method",
        "route",
        "status_class",
        "workflow_type",
        "step_type",
        "alert_name",
        "severity",
        "alert_state",
        "receiver",
        "dataset",
        "category",
        "failure_category",
        "ablation",
    }
)
FORBIDDEN_METRIC_LABELS = frozenset(
    {
        "user_id",
        "email",
        "request_id",
        "correlation_id",
        "run_id",
        "step_id",
        "plan_id",
        "session_id",
        "draft_id",
        "memory_id",
        "calendar_id",
        "external_event_id",
        "operation_key",
        "url",
        "error_message",
    }
)
DEFAULT_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


class MetricKind(StrEnum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"
    GAUGE = "gauge"


@dataclass(frozen=True, slots=True)
class MetricDescriptor:
    name: str
    description: str
    kind: MetricKind
    labels: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.name or not self.description:
            raise ValueError("Metric name and description must not be blank.")
        labels = set(self.labels)
        if len(labels) != len(self.labels):
            raise ValueError(f"Metric {self.name} contains duplicate labels.")
        forbidden = labels & FORBIDDEN_METRIC_LABELS
        unknown = labels - ALLOWED_METRIC_LABELS
        if forbidden or unknown:
            invalid = sorted(forbidden | unknown)
            raise ValueError(f"Metric {self.name} has forbidden labels: {invalid}")


def _counter(name: str, *labels: str) -> MetricDescriptor:
    return MetricDescriptor(name, name.replace("_", " "), MetricKind.COUNTER, labels)


def _histogram(name: str, *labels: str) -> MetricDescriptor:
    return MetricDescriptor(name, name.replace("_", " "), MetricKind.HISTOGRAM, labels)


def _gauge(name: str, *labels: str) -> MetricDescriptor:
    return MetricDescriptor(name, name.replace("_", " "), MetricKind.GAUGE, labels)


DEFAULT_METRIC_DESCRIPTORS: tuple[MetricDescriptor, ...] = (
    _counter("http_server_requests_total", "method", "route", "status_class"),
    _histogram(
        "http_server_request_duration_seconds", "method", "route", "status_class"
    ),
    _gauge("http_server_requests_in_progress", "method", "route"),
    _counter("fitweek_agent_runs_total", "agent_type", "outcome"),
    _histogram("fitweek_agent_run_duration_seconds", "agent_type", "outcome"),
    _counter("fitweek_agent_retries_total", "agent_type", "outcome"),
    _counter("fitweek_agent_fallbacks_total", "agent_type", "outcome"),
    _counter("fitweek_agent_schema_repairs_total", "agent_type", "outcome"),
    _counter("fitweek_agent_human_interventions_total", "agent_type", "outcome"),
    _counter("fitweek_agent_loop_limits_total", "agent_type", "outcome"),
    _counter("fitweek_model_invocations_total", "provider_name", "outcome"),
    _counter("fitweek_model_attempts_total", "provider_name", "outcome"),
    _counter("fitweek_model_timeouts_total", "provider_name", "outcome"),
    _counter("fitweek_model_rate_limits_total", "provider_name", "outcome"),
    _counter("fitweek_model_5xx_total", "provider_name", "outcome"),
    _counter("fitweek_model_schema_invalid_total", "provider_name", "outcome"),
    _counter("fitweek_model_backup_used_total", "provider_name", "outcome"),
    _counter("fitweek_model_fallback_total", "provider_name", "outcome"),
    _histogram("fitweek_model_duration_seconds", "provider_name", "outcome"),
    _counter(
        "fitweek_tool_invocations_total",
        "tool_id",
        "tool_version",
        "provider_name",
        "outcome",
        "error_category",
        "degradation_mode",
    ),
    _counter(
        "fitweek_tool_attempts_total",
        "tool_id",
        "tool_version",
        "provider_name",
        "outcome",
        "error_category",
    ),
    _counter(
        "fitweek_tool_retries_total",
        "tool_id",
        "tool_version",
        "provider_name",
        "outcome",
    ),
    _counter(
        "fitweek_tool_successes_total", "tool_id", "tool_version", "provider_name"
    ),
    _counter(
        "fitweek_tool_failures_total",
        "tool_id",
        "tool_version",
        "provider_name",
        "error_category",
    ),
    _counter("fitweek_tool_timeouts_total", "tool_id", "tool_version", "provider_name"),
    _counter(
        "fitweek_tool_rate_limits_total", "tool_id", "tool_version", "provider_name"
    ),
    _counter(
        "fitweek_tool_circuit_open_total", "tool_id", "tool_version", "provider_name"
    ),
    _counter(
        "fitweek_tool_circuit_rejections_total",
        "tool_id",
        "tool_version",
        "provider_name",
    ),
    _counter(
        "fitweek_tool_bulkhead_rejections_total",
        "tool_id",
        "tool_version",
        "provider_name",
    ),
    _counter(
        "fitweek_tool_deadline_exceeded_total",
        "tool_id",
        "tool_version",
        "provider_name",
    ),
    _counter(
        "fitweek_tool_retry_budget_exhausted_total",
        "tool_id",
        "tool_version",
        "provider_name",
    ),
    _counter(
        "fitweek_tool_degraded_total",
        "tool_id",
        "tool_version",
        "provider_name",
        "degradation_mode",
    ),
    _histogram(
        "fitweek_tool_duration_seconds",
        "tool_id",
        "tool_version",
        "provider_name",
        "outcome",
    ),
    _counter("fitweek_memory_queries_total", "outcome"),
    _counter("fitweek_memory_query_failures_total", "outcome"),
    _counter("fitweek_memory_no_memory_total", "outcome"),
    _counter("fitweek_memory_candidates_created_total", "outcome"),
    _counter("fitweek_memory_candidates_accepted_total", "outcome"),
    _counter("fitweek_memory_expired_recall_total", "outcome"),
    _counter("fitweek_memory_deleted_recall_total", "outcome"),
    _counter("fitweek_plan_generations_total", "outcome"),
    _counter("fitweek_plan_confirmations_total", "outcome"),
    _counter("fitweek_schedule_drafts_total", "outcome"),
    _counter("fitweek_calendar_operations_total", "outcome"),
    _counter("fitweek_ics_exports_total", "outcome"),
    _counter("fitweek_checkins_total", "outcome"),
    _counter("fitweek_recovery_drafts_total", "outcome"),
    _counter("fitweek_recovery_applications_total", "outcome"),
    _counter("fitweek_plan_revisions_total", "outcome"),
    _counter("fitweek_observability_degraded_total", "component", "outcome"),
    _gauge("fitweek_metrics_endpoint_available", "component"),
    _counter("fitweek_structured_log_sink_failures_total", "component", "outcome"),
    _counter("fitweek_orchestrator_runs_total", "workflow_type", "outcome"),
    _counter("fitweek_orchestrator_steps_total", "step_type", "outcome"),
    _histogram("fitweek_orchestrator_step_duration_seconds", "step_type", "outcome"),
    _counter("fitweek_orchestrator_step_retries_total", "step_type", "outcome"),
    _counter("fitweek_orchestrator_leases_recovered_total", "component", "outcome"),
    _counter("fitweek_orchestrator_reaper_recoveries_total", "component", "outcome"),
    _counter(
        "fitweek_orchestrator_worker_interruptions_recovered_total",
        "component",
        "outcome",
    ),
    _gauge("fitweek_orchestrator_waiting_runs", "workflow_type"),
    _gauge("fitweek_orchestrator_backlog", "workflow_type"),
    _gauge("fitweek_orchestrator_oldest_waiting_seconds", "workflow_type"),
    _counter(
        "fitweek_alert_evaluations_total",
        "alert_name",
        "severity",
        "component",
        "outcome",
    ),
    _gauge(
        "fitweek_alert_state",
        "alert_name",
        "severity",
        "component",
        "alert_state",
    ),
    _counter(
        "fitweek_alert_notifications_total",
        "alert_name",
        "severity",
        "component",
        "receiver",
        "outcome",
    ),
    _counter("fitweek_alert_receiver_failures_total", "receiver", "outcome"),
    _counter("fitweek_alert_inhibitions_total", "component", "outcome"),
    _counter("fitweek_alert_silences_total", "component", "outcome"),
    _counter(
        "fitweek_evaluation_cases_total",
        "dataset",
        "category",
        "outcome",
        "failure_category",
        "ablation",
    ),
    _counter(
        "fitweek_evaluation_failures_total",
        "dataset",
        "category",
        "failure_category",
        "ablation",
    ),
    _histogram(
        "fitweek_evaluation_duration_seconds",
        "dataset",
        "category",
        "outcome",
        "ablation",
    ),
    _counter(
        "fitweek_evaluation_gate_failures_total",
        "dataset",
        "outcome",
        "failure_category",
        "ablation",
    ),
)


class PrometheusMetricSink:
    """Process-local registry. Recording failures never escape into business code."""

    def __init__(
        self,
        *,
        enabled: bool,
        descriptors: Sequence[MetricDescriptor] = DEFAULT_METRIC_DESCRIPTORS,
        registry: CollectorRegistry | None = None,
    ) -> None:
        self.enabled = enabled
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.descriptors = tuple(descriptors)
        self._metrics: dict[str, Counter | Gauge | Histogram] = {}
        self._degraded = False
        self._lock = Lock()
        for descriptor in self.descriptors:
            descriptor.validate()
        if not enabled:
            return
        try:
            for descriptor in self.descriptors:
                if descriptor.kind is MetricKind.COUNTER:
                    metric: Counter | Gauge | Histogram = Counter(
                        descriptor.name,
                        descriptor.description,
                        descriptor.labels,
                        registry=self.registry,
                    )
                elif descriptor.kind is MetricKind.GAUGE:
                    metric = Gauge(
                        descriptor.name,
                        descriptor.description,
                        descriptor.labels,
                        registry=self.registry,
                    )
                else:
                    metric = Histogram(
                        descriptor.name,
                        descriptor.description,
                        descriptor.labels,
                        buckets=DEFAULT_BUCKETS,
                        registry=self.registry,
                    )
                self._metrics[descriptor.name] = metric
        except Exception:
            self._degraded = True
            self._metrics.clear()

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _child(
        self, name: str, labels: Mapping[str, str]
    ) -> Counter | Gauge | Histogram | None:
        metric = self._metrics.get(name)
        descriptor = next(
            (item for item in self.descriptors if item.name == name), None
        )
        if metric is None or descriptor is None:
            return None
        if set(labels) != set(descriptor.labels):
            self._degraded = True
            return None
        return metric.labels(**dict(labels)) if labels else metric

    def counter(
        self, name: str, value: float = 1, *, labels: Mapping[str, str] | None = None
    ) -> None:
        if not self.enabled:
            return
        try:
            child = self._child(name, labels or {})
            if isinstance(child, Counter):
                child.inc(value)
        except Exception:
            self._degraded = True

    def histogram(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        if not self.enabled:
            return
        try:
            child = self._child(name, labels or {})
            if isinstance(child, Histogram):
                child.observe(value)
        except Exception:
            self._degraded = True

    def gauge_add(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        if not self.enabled:
            return
        try:
            child = self._child(name, labels or {})
            if isinstance(child, Gauge):
                with self._lock:
                    child.inc(value)
        except Exception:
            self._degraded = True

    def render(self) -> bytes:
        if not self.enabled:
            return b""
        try:
            return generate_latest(self.registry)
        except Exception:
            self._degraded = True
            return b""
