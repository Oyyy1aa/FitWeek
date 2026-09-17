"""Central retry policy and correlation-scoped in-memory retry budget."""

from dataclasses import dataclass
from uuid import UUID

from app.domain.tools.enums import ToolErrorCategory, ToolSideEffectClass

RETRYABLE = frozenset(
    {
        ToolErrorCategory.TIMEOUT,
        ToolErrorCategory.CONNECTION,
        ToolErrorCategory.RATE_LIMIT,
        ToolErrorCategory.UPSTREAM_5XX,
    }
)


@dataclass(slots=True)
class ToolRetryBudget:
    correlation_id: UUID
    maximum_total_attempts: int = 12
    consumed_attempts: int = 0

    def consume(self) -> bool:
        if self.consumed_attempts >= self.maximum_total_attempts:
            return False
        self.consumed_attempts += 1
        return True


class RetryBudgetRegistry:
    def __init__(self, maximum_total_attempts: int = 12) -> None:
        if maximum_total_attempts < 1:
            raise ValueError("maximum_total_attempts must be positive")
        self._maximum_total_attempts = maximum_total_attempts
        self._budgets: dict[UUID, ToolRetryBudget] = {}

    def budget_for(self, correlation_id: UUID) -> ToolRetryBudget:
        return self._budgets.setdefault(
            correlation_id,
            ToolRetryBudget(
                correlation_id,
                maximum_total_attempts=self._maximum_total_attempts,
            ),
        )


def is_retryable(category: ToolErrorCategory) -> bool:
    return category in RETRYABLE


def backoff_ms(attempt_number: int) -> int:
    return {1: 0, 2: 100, 3: 300}.get(attempt_number, 300)


def permitted_attempts(effect: ToolSideEffectClass, configured: int) -> int:
    if effect in {
        ToolSideEffectClass.PURE,
        ToolSideEffectClass.INTERNAL_WRITE,
        ToolSideEffectClass.FILE_WRITE,
    }:
        return 1
    return min(configured, 3)
