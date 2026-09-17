"""Bounded in-process counters and histogram samples with low-cardinality keys."""

from collections import Counter, defaultdict, deque

from app.domain.tools.enums import (
    CircuitState,
    ToolDegradationMode,
    ToolErrorCategory,
    ToolId,
    ToolInvocationStatus,
)


class ToolMetrics:
    def __init__(self, sample_limit: int = 512) -> None:
        self._counts: Counter[str] = Counter()
        self._latencies: defaultdict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=sample_limit)
        )

    def record(self, key: str, latency_ms: float) -> None:
        """Compatibility counter used by early Phase 8A callers."""

        self._counts[key] += 1
        self._latencies[key].append(latency_ms)

    def record_attempt(
        self,
        *,
        tool_id: ToolId,
        status: str,
        error_category: ToolErrorCategory | None,
        latency_ms: float,
        attempt_no: int,
    ) -> None:
        self._counts["tool_attempts_total"] += 1
        if attempt_no > 1:
            self._counts["tool_retries_total"] += 1
        if error_category is ToolErrorCategory.TIMEOUT:
            self._counts["tool_timeouts_total"] += 1
        if error_category is ToolErrorCategory.RATE_LIMIT:
            self._counts["tool_rate_limits_total"] += 1
        key = (
            f"attempt|{tool_id.value}|{status}|"
            f"{(error_category.value if error_category else 'NONE')}"
        )
        self._counts[key] += 1
        self._latencies[f"attempt|{tool_id.value}"].append(latency_ms)

    def record_invocation(
        self,
        *,
        tool_id: ToolId,
        status: ToolInvocationStatus,
        error_category: ToolErrorCategory | None,
        error_code: str | None,
        degradation: ToolDegradationMode,
        circuit_before: CircuitState,
        circuit_after: CircuitState,
        latency_ms: float,
    ) -> None:
        self._counts["tool_invocations_total"] += 1
        if status is ToolInvocationStatus.SUCCEEDED:
            self._counts["tool_successes_total"] += 1
        elif status is not ToolInvocationStatus.DEGRADED:
            self._counts["tool_failures_total"] += 1
        if error_category is ToolErrorCategory.CIRCUIT_OPEN:
            self._counts["tool_circuit_rejections_total"] += 1
        if error_category is ToolErrorCategory.BULKHEAD_REJECTED:
            self._counts["tool_bulkhead_rejections_total"] += 1
        if error_category is ToolErrorCategory.DEADLINE_EXCEEDED:
            self._counts["tool_deadline_exceeded_total"] += 1
        if error_code == "TOOL_RETRY_BUDGET_EXHAUSTED":
            self._counts["tool_retry_budget_exhausted_total"] += 1
        if (
            circuit_before is not CircuitState.OPEN
            and circuit_after is CircuitState.OPEN
        ):
            self._counts["tool_circuit_open_total"] += 1
        if circuit_before is CircuitState.HALF_OPEN:
            self._counts["tool_circuit_half_open_total"] += 1
        if degradation is not ToolDegradationMode.NONE:
            if status is ToolInvocationStatus.DEGRADED:
                self._counts["tool_degraded_success_total"] += 1
            else:
                self._counts["tool_degradation_failures_total"] += 1
        key = (
            f"invocation|{tool_id.value}|{status.value}|"
            f"{(error_category.value if error_category else 'NONE')}|"
            f"{degradation.value}"
        )
        self._counts[key] += 1
        self._latencies[f"invocation|{tool_id.value}"].append(latency_ms)

    def record_memory_query_bridge(
        self, *, attempt_no: int, succeeded: bool, degraded: bool
    ) -> None:
        """Record NO_MEMORY compatibility without pretending it is a Tool success."""

        self._counts["memory_query_bridge_attempts_total"] += 1
        if attempt_no > 1:
            self._counts["memory_query_bridge_retries_total"] += 1
        if succeeded:
            self._counts["memory_query_bridge_successes_total"] += 1
        else:
            self._counts["memory_query_bridge_failures_total"] += 1
        if degraded:
            self._counts["tool_degradation_failures_total"] += 1
            self._counts["memory_query_no_memory_total"] += 1

    def record_stale_data_rejected(self) -> None:
        self._counts["stale_data_rejected_total"] += 1
        self._counts["tool_degradation_failures_total"] += 1

    def snapshot(self) -> dict[str, object]:
        quantiles: dict[str, dict[str, float]] = {}
        for key, values in self._latencies.items():
            ordered = sorted(values)
            if ordered:
                quantiles[key] = {
                    label: ordered[
                        min(len(ordered) - 1, int((len(ordered) - 1) * point))
                    ]
                    for label, point in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
                }
        return {"counts": dict(self._counts), "latency_ms": quantiles}
