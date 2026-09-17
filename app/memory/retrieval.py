"""Bounded Memory retrieval with a single retry and explicit degradation."""

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from redis.exceptions import RedisError

from app.domain.context.enums import ContextDegradedMode
from app.domain.memory.errors import MemoryQueryFailedError
from app.domain.memory.models import MemoryQueryResult
from app.domain.memory.repositories import MemoryRepository
from app.domain.tools.enums import ToolDegradationMode, ToolErrorCategory
from app.memory.cache import MemoryActiveCache
from app.memory.metrics import MemoryMetrics
from app.observability.context import (
    ObservabilityContext,
    current_observability_context,
)
from app.observability.facade import ObservabilityFacade
from app.tool_gateway.metrics import ToolMetrics
from app.tool_gateway.traces import (
    ReliabilityCompatibilityTrace,
    ToolTraceStore,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrievalOutcome:
    result: MemoryQueryResult
    degraded_mode: ContextDegradedMode


class MemoryRetriever:
    def __init__(
        self,
        repository: MemoryRepository,
        metrics: MemoryMetrics,
        cache: MemoryActiveCache | None = None,
    ) -> None:
        self._repository = repository
        self._metrics = metrics
        self._cache = cache
        self._reliability_traces: ToolTraceStore | None = None
        self._reliability_metrics: ToolMetrics | None = None
        self._observability: ObservabilityFacade | None = None

    def attach_reliability(self, traces: ToolTraceStore, metrics: ToolMetrics) -> None:
        self._reliability_traces = traces
        self._reliability_metrics = metrics

    def attach_observability(self, observability: ObservabilityFacade) -> None:
        self._observability = observability

    async def retrieve(self, user_id: UUID, now: datetime) -> RetrievalOutcome:
        if self._observability is None:
            return await self._retrieve(user_id, now)
        inherited = current_observability_context()
        context = ObservabilityContext(
            correlation_id=(
                inherited.correlation_id if inherited is not None else uuid4()
            ),
            request_id=inherited.request_id if inherited is not None else None,
            run_id=inherited.run_id if inherited is not None else None,
            step_id=inherited.step_id if inherited is not None else None,
            operation_name="memory.query",
            component="memory",
        )
        with self._observability.operation("memory.query", context=context) as span:
            outcome = await self._retrieve(user_id, now)
            degraded = outcome.degraded_mode is ContextDegradedMode.NO_MEMORY
            result_name = "NO_MEMORY" if degraded else "SUCCEEDED"
            self._observability.record_counter(
                "fitweek_memory_queries_total", labels={"outcome": result_name}
            )
            if degraded:
                span.degrade("NO_MEMORY")
                span.reject("DEGRADED", "NO_MEMORY")
                self._observability.record_counter(
                    "fitweek_memory_query_failures_total",
                    labels={"outcome": "NO_MEMORY"},
                )
                self._observability.record_counter(
                    "fitweek_memory_no_memory_total",
                    labels={"outcome": "DEGRADED"},
                )
            else:
                span.succeed()
            return outcome

    async def _retrieve(self, user_id: UUID, now: datetime) -> RetrievalOutcome:
        self._metrics.increment("memory_queries")
        inherited = current_observability_context()
        correlation_id = inherited.correlation_id if inherited is not None else uuid4()
        if self._cache is not None:
            try:
                cached = await self._cache.get_active(user_id)
            except (RedisError, OSError, TimeoutError):
                self._metrics.increment("memory_cache_degraded")
                logger.warning("memory_cache_degraded", extra={"operation": "get"})
                cached = None
            if cached is not None:
                return RetrievalOutcome(
                    result=MemoryQueryResult(
                        memories=cached,
                        expired_filtered=0,
                        deleted_filtered=0,
                        pending_filtered=0,
                        filtered_reasons={},
                    ),
                    degraded_mode=ContextDegradedMode.NONE,
                )
        for attempt in range(2):
            try:
                result = await self._repository.list_active_for_context(user_id, now)
                if self._cache is not None:
                    try:
                        await self._cache.set_active(user_id, result.memories)
                    except (RedisError, OSError, TimeoutError):
                        self._metrics.increment("memory_cache_degraded")
                        logger.warning(
                            "memory_cache_degraded", extra={"operation": "set"}
                        )
                self._record_reliability(
                    user_id=user_id,
                    correlation_id=correlation_id,
                    attempt_no=attempt + 1,
                    succeeded=True,
                    degraded=False,
                    now=now,
                )
                self._metrics.increment(
                    "expired_memories_filtered", result.expired_filtered
                )
                self._metrics.increment(
                    "deleted_memories_filtered", result.deleted_filtered
                )
                self._metrics.increment(
                    "pending_memories_filtered", result.pending_filtered
                )
                return RetrievalOutcome(
                    result=result, degraded_mode=ContextDegradedMode.NONE
                )
            except MemoryQueryFailedError:
                self._metrics.increment("memory_query_failures")
                final = attempt == 1
                self._record_reliability(
                    user_id=user_id,
                    correlation_id=correlation_id,
                    attempt_no=attempt + 1,
                    succeeded=False,
                    degraded=final,
                    now=now,
                )
                if attempt == 1:
                    self._metrics.increment("context_no_memory_degraded")
                    return RetrievalOutcome(
                        result=MemoryQueryResult(
                            memories=(),
                            expired_filtered=0,
                            deleted_filtered=0,
                            pending_filtered=0,
                            filtered_reasons={},
                        ),
                        degraded_mode=ContextDegradedMode.NO_MEMORY,
                    )
        raise AssertionError("bounded retrieval loop must return")

    def _record_reliability(
        self,
        *,
        user_id: UUID,
        correlation_id: UUID,
        attempt_no: int,
        succeeded: bool,
        degraded: bool,
        now: datetime,
    ) -> None:
        if self._reliability_metrics is not None:
            self._reliability_metrics.record_memory_query_bridge(
                attempt_no=attempt_no,
                succeeded=succeeded,
                degraded=degraded,
            )
        if self._reliability_traces is not None:
            self._reliability_traces.add_compatibility(
                ReliabilityCompatibilityTrace(
                    correlation_id=correlation_id,
                    user_id=user_id,
                    component="MEMORY_QUERY",
                    attempt_no=attempt_no,
                    status="SUCCEEDED" if succeeded else "FAILED",
                    error_category=(
                        None if succeeded else ToolErrorCategory.INTERNAL_ERROR
                    ),
                    error_code=None if succeeded else "MEMORY_QUERY_FAILED",
                    degradation_mode=(
                        ToolDegradationMode.NO_MEMORY
                        if degraded
                        else ToolDegradationMode.NONE
                    ),
                    created_at=now,
                )
            )
