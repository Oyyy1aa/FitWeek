"""Prometheus descriptors, bounded histograms, and cardinality guards."""

from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry

from app.observability.metrics import (
    ALLOWED_METRIC_LABELS,
    DEFAULT_BUCKETS,
    DEFAULT_METRIC_DESCRIPTORS,
    FORBIDDEN_METRIC_LABELS,
    MetricDescriptor,
    MetricKind,
    PrometheusMetricSink,
)

pytestmark = pytest.mark.phase_8b1


def _sink() -> PrometheusMetricSink:
    return PrometheusMetricSink(enabled=True)


def _text(sink: PrometheusMetricSink) -> str:
    return sink.render().decode("utf-8")


def test_http_metric_descriptors_are_registered() -> None:
    names = {item.name for item in DEFAULT_METRIC_DESCRIPTORS}
    assert {
        "http_server_requests_total",
        "http_server_request_duration_seconds",
        "http_server_requests_in_progress",
    } <= names


def test_agent_metric_descriptors_are_registered() -> None:
    names = {item.name for item in DEFAULT_METRIC_DESCRIPTORS}
    assert "fitweek_agent_runs_total" in names
    assert "fitweek_agent_loop_limits_total" in names


def test_model_metric_descriptors_exclude_unreliable_cost_data() -> None:
    names = {item.name for item in DEFAULT_METRIC_DESCRIPTORS}
    assert "fitweek_model_invocations_total" in names
    assert not any("token" in name or "cost" in name for name in names)


def test_tool_metric_descriptors_map_phase_8a_semantics() -> None:
    names = {item.name for item in DEFAULT_METRIC_DESCRIPTORS}
    assert "fitweek_tool_invocations_total" in names
    assert "fitweek_tool_attempts_total" in names
    assert "fitweek_tool_retry_budget_exhausted_total" in names


def test_memory_metric_descriptors_are_registered() -> None:
    names = {item.name for item in DEFAULT_METRIC_DESCRIPTORS}
    assert "fitweek_memory_queries_total" in names
    assert "fitweek_memory_deleted_recall_total" in names


def test_business_metric_descriptors_are_registered() -> None:
    names = {item.name for item in DEFAULT_METRIC_DESCRIPTORS}
    assert "fitweek_plan_generations_total" in names
    assert "fitweek_recovery_applications_total" in names


def test_counter_exports_low_cardinality_labels() -> None:
    sink = _sink()
    sink.counter("fitweek_plan_generations_total", labels={"outcome": "SUCCEEDED"})
    text = _text(sink)
    assert 'fitweek_plan_generations_total{outcome="SUCCEEDED"} 1.0' in text


def test_histogram_exports_fixed_buckets() -> None:
    sink = _sink()
    labels = {"method": "GET", "route": "/health/live", "status_class": "2xx"}
    sink.histogram("http_server_request_duration_seconds", 0.02, labels=labels)
    text = _text(sink)
    assert "http_server_request_duration_seconds_bucket" in text
    assert len(DEFAULT_BUCKETS) == 12


def test_gauge_can_increment_and_release() -> None:
    sink = _sink()
    labels = {"method": "GET", "route": "/metrics"}
    sink.gauge_add("http_server_requests_in_progress", 1, labels=labels)
    sink.gauge_add("http_server_requests_in_progress", -1, labels=labels)
    assert (
        'http_server_requests_in_progress{method="GET",route="/metrics"} 0.0'
        in _text(sink)
    )


def test_forbidden_metric_label_fails_registration() -> None:
    descriptor = MetricDescriptor("bad_total", "bad", MetricKind.COUNTER, ("user_id",))
    with pytest.raises(ValueError, match="forbidden labels"):
        PrometheusMetricSink(enabled=True, descriptors=(descriptor,))


def test_unknown_metric_label_fails_registration() -> None:
    descriptor = MetricDescriptor(
        "bad_total", "bad", MetricKind.COUNTER, ("unbounded",)
    )
    with pytest.raises(ValueError, match="forbidden labels"):
        PrometheusMetricSink(enabled=True, descriptors=(descriptor,))


def test_duplicate_metric_labels_fail_registration() -> None:
    descriptor = MetricDescriptor(
        "bad_total", "bad", MetricKind.COUNTER, ("outcome", "outcome")
    )
    with pytest.raises(ValueError, match="duplicate labels"):
        PrometheusMetricSink(enabled=True, descriptors=(descriptor,))


def test_recording_with_wrong_label_set_is_isolated() -> None:
    sink = _sink()
    sink.counter(
        "fitweek_plan_generations_total",
        labels={"outcome": "OK", "component": "extra"},
    )
    assert sink.degraded is True


def test_disabled_metrics_render_empty() -> None:
    sink = PrometheusMetricSink(enabled=False)
    sink.counter("fitweek_plan_generations_total", labels={"outcome": "OK"})
    assert sink.render() == b""


def test_distinct_request_uuids_cannot_create_metric_series() -> None:
    sink = _sink()
    for _ in range(20):
        request_id = str(uuid4())
        assert request_id
        sink.counter("fitweek_plan_generations_total", labels={"outcome": "SUCCEEDED"})
    series = [
        line
        for line in _text(sink).splitlines()
        if line.startswith("fitweek_plan_generations_total{")
    ]
    assert len(series) == 1
    assert series[0].endswith("20.0")


def test_allowed_and_forbidden_label_sets_do_not_overlap() -> None:
    assert ALLOWED_METRIC_LABELS.isdisjoint(FORBIDDEN_METRIC_LABELS)


def test_custom_registry_is_process_local() -> None:
    first = PrometheusMetricSink(enabled=True, registry=CollectorRegistry())
    second = PrometheusMetricSink(enabled=True, registry=CollectorRegistry())
    first.counter("fitweek_checkins_total", labels={"outcome": "OK"})
    assert 'fitweek_checkins_total{outcome="OK"}' in _text(first)
    assert 'fitweek_checkins_total{outcome="OK"}' not in _text(second)
