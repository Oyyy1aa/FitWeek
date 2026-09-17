"""Safe read-only operational visibility for the in-memory Tool Gateway."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.dependencies import get_current_user, get_tool_gateway
from app.domain.users.models import UserAccount
from app.tool_gateway.gateway import ToolGateway

router = APIRouter(prefix="/tool-gateway", tags=["tool-gateway"])


class ToolDescriptorResponse(BaseModel):
    tool_id: str
    version: str
    side_effect_class: str
    allowed_callers: tuple[str, ...]
    timeout_ms: int
    deadline_budget_ms: int
    max_attempts: int
    bulkhead_limit: int


@router.get("/tools", response_model=tuple[ToolDescriptorResponse, ...])
async def tools(
    gateway: Annotated[ToolGateway, Depends(get_tool_gateway)],
) -> tuple[ToolDescriptorResponse, ...]:
    return tuple(
        ToolDescriptorResponse(
            tool_id=item.descriptor.tool_id.value,
            version=item.descriptor.version,
            side_effect_class=item.descriptor.side_effect_class.value,
            allowed_callers=tuple(
                sorted(caller.value for caller in item.descriptor.allowed_callers)
            ),
            timeout_ms=item.descriptor.default_timeout_ms,
            deadline_budget_ms=item.descriptor.request_deadline_budget_ms,
            max_attempts=item.descriptor.max_attempts,
            bulkhead_limit=item.descriptor.bulkhead_limit,
        )
        for item in gateway.registry.registrations()
    )


@router.get("/status")
async def gateway_status(
    gateway: Annotated[ToolGateway, Depends(get_tool_gateway)],
) -> dict[str, object]:
    open_circuits = await gateway.circuits.open_keys()
    return {
        "status": "DEGRADED" if open_circuits else "AVAILABLE",
        "critical_tools_registered": len(gateway.registry.registrations()) == 7,
        "degraded_tools": [],
        "open_circuits": open_circuits,
    }


@router.get("/circuits")
async def circuits(
    gateway: Annotated[ToolGateway, Depends(get_tool_gateway)],
) -> dict[str, tuple[str, ...]]:
    return {"open_circuits": await gateway.circuits.open_keys()}


@router.get("/metrics")
async def metrics(
    gateway: Annotated[ToolGateway, Depends(get_tool_gateway)],
) -> dict[str, object]:
    return gateway.metrics.snapshot()


@router.get("/traces")
async def recent_traces(
    gateway: Annotated[ToolGateway, Depends(get_tool_gateway)],
    user: Annotated[UserAccount, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=100),
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "invocation_id": str(item.invocation_id),
            "correlation_id": str(item.correlation_id),
            "tool_id": item.tool_id.value,
            "tool_version": item.tool_version,
            "provider_name": item.provider_name,
            "attempt_no": item.attempt_no,
            "status": item.status,
            "error_category": item.error_category,
            "error_code": item.error_code,
            "latency_ms": item.latency_ms,
            "circuit_before": item.circuit_before,
            "circuit_after": item.circuit_after,
            "degradation_mode": item.degradation_mode,
            "created_at": item.created_at,
        }
        for item in gateway.traces.list_for_user(user.id, limit=limit)
    )


@router.get("/invocations")
async def recent_invocations(
    gateway: Annotated[ToolGateway, Depends(get_tool_gateway)],
    user: Annotated[UserAccount, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=100),
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "invocation_id": str(item.invocation_id),
            "correlation_id": str(item.correlation_id),
            "tool_id": item.tool_id.value,
            "tool_version": item.tool_version,
            "caller": item.caller,
            "attempt_count": item.attempt_count,
            "status": item.status,
            "error_category": item.error_category,
            "error_code": item.error_code,
            "latency_ms": item.latency_ms,
            "circuit_before": item.circuit_before,
            "circuit_after": item.circuit_after,
            "degradation_mode": item.degradation_mode,
            "created_at": item.created_at,
        }
        for item in gateway.traces.summaries_for_user(user.id, limit=limit)
    )


@router.get("/traces/{correlation_id}")
async def traces(
    correlation_id: UUID,
    gateway: Annotated[ToolGateway, Depends(get_tool_gateway)],
    user: Annotated[UserAccount, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=100),
) -> tuple[dict[str, object], ...]:
    # Invocation payloads and provider URLs are intentionally absent from traces.
    return tuple(
        {
            "invocation_id": str(item.invocation_id),
            "tool_id": item.tool_id.value,
            "tool_version": item.tool_version,
            "provider_name": item.provider_name,
            "attempt_no": item.attempt_no,
            "status": item.status,
            "error_category": item.error_category,
            "error_code": item.error_code,
            "latency_ms": item.latency_ms,
            "circuit_before": item.circuit_before,
            "circuit_after": item.circuit_after,
            "degradation_mode": item.degradation_mode,
            "created_at": item.created_at,
        }
        for item in gateway.traces.by_correlation_for_user(
            user.id, correlation_id, limit
        )
    )


@router.get("/invocations/{correlation_id}")
async def invocations(
    correlation_id: UUID,
    gateway: Annotated[ToolGateway, Depends(get_tool_gateway)],
    user: Annotated[UserAccount, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=100),
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "invocation_id": str(item.invocation_id),
            "tool_id": item.tool_id.value,
            "tool_version": item.tool_version,
            "caller": item.caller,
            "attempt_count": item.attempt_count,
            "status": item.status,
            "error_category": item.error_category,
            "error_code": item.error_code,
            "latency_ms": item.latency_ms,
            "circuit_before": item.circuit_before,
            "circuit_after": item.circuit_after,
            "degradation_mode": item.degradation_mode,
            "created_at": item.created_at,
        }
        for item in gateway.traces.summaries_by_correlation_for_user(
            user.id, correlation_id, limit
        )
    )


@router.get("/compatibility-traces")
async def compatibility_traces(
    gateway: Annotated[ToolGateway, Depends(get_tool_gateway)],
    user: Annotated[UserAccount, Depends(get_current_user)],
    correlation_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=100),
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "correlation_id": str(item.correlation_id),
            "component": item.component,
            "attempt_no": item.attempt_no,
            "status": item.status,
            "error_category": item.error_category,
            "error_code": item.error_code,
            "degradation_mode": item.degradation_mode,
            "created_at": item.created_at,
        }
        for item in gateway.traces.compatibility_for_user(
            user.id, correlation_id=correlation_id, limit=limit
        )
    )
