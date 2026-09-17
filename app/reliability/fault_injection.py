"""Test-only in-process faults. This module has no public API wiring."""

from dataclasses import dataclass
from enum import StrEnum

from app.domain.tools.enums import ToolErrorCategory, ToolId


class FaultType(StrEnum):
    TIMEOUT = "TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    RATE_LIMIT = "RATE_LIMIT"
    UPSTREAM_5XX = "UPSTREAM_5XX"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    AUTHORIZATION_ERROR = "AUTHORIZATION_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    OVERSIZED_RESPONSE = "OVERSIZED_RESPONSE"
    STALE_DATA = "STALE_DATA"
    RESPONSE_LOST_AFTER_SIDE_EFFECT = "RESPONSE_LOST_AFTER_SIDE_EFFECT"


@dataclass(frozen=True, slots=True)
class FaultInjectionPlan:
    fault_type: FaultType
    tool_id: ToolId
    fail_attempts: frozenset[int]
    latency_ms: int | None = None
    error_category: ToolErrorCategory | None = None


class FaultInjector:
    """Internal fixture helper; never constructed from request/configuration data."""

    def __init__(self, plans: tuple[FaultInjectionPlan, ...] = ()) -> None:
        self._plans = plans

    def error_for(self, tool_id: ToolId, attempt: int) -> ToolErrorCategory | None:
        for plan in self._plans:
            if plan.tool_id is tool_id and attempt in plan.fail_attempts:
                if plan.error_category is not None:
                    return plan.error_category
                return {
                    FaultType.TIMEOUT: ToolErrorCategory.TIMEOUT,
                    FaultType.CONNECTION_ERROR: ToolErrorCategory.CONNECTION,
                    FaultType.RATE_LIMIT: ToolErrorCategory.RATE_LIMIT,
                    FaultType.UPSTREAM_5XX: ToolErrorCategory.UPSTREAM_5XX,
                    FaultType.AUTHENTICATION_ERROR: ToolErrorCategory.AUTHENTICATION,
                    FaultType.AUTHORIZATION_ERROR: ToolErrorCategory.AUTHORIZATION,
                    FaultType.INVALID_RESPONSE: ToolErrorCategory.RESPONSE_INVALID,
                    FaultType.OVERSIZED_RESPONSE: ToolErrorCategory.RESPONSE_TOO_LARGE,
                    FaultType.STALE_DATA: ToolErrorCategory.STALE_DATA,
                    FaultType.RESPONSE_LOST_AFTER_SIDE_EFFECT: (
                        ToolErrorCategory.CONNECTION
                    ),
                }[plan.fault_type]
        return None
