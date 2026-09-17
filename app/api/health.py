"""Liveness and readiness endpoints."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import PersistenceBackend, Settings, get_settings
from app.infrastructure.redis_client import RedisManager, get_redis_manager
from app.persistence.database import (
    Database,
    check_database_connection,
    get_database,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: str
    service: str


class ReadyResponse(BaseModel):
    status: str
    checks: dict[str, str]
    mode: str | None = None
    warnings: list[str] | None = None
    tool_gateway: dict[str, object] | None = None
    observability: dict[str, object] | None = None
    alerting: dict[str, object] | None = None


async def _mysql_status(database: Database) -> str:
    try:
        available = await check_database_connection(database)
    except Exception as exc:
        logger.warning(
            "mysql_readiness_failed",
            extra={"error_type": type(exc).__name__},
        )
        return "unavailable"
    return "ok" if available else "unavailable"


async def _redis_status(redis_manager: RedisManager) -> str:
    if not redis_manager.enabled:
        return "disabled"
    try:
        available = await redis_manager.ping()
    except Exception as exc:
        logger.warning(
            "redis_readiness_failed",
            extra={"error_type": type(exc).__name__},
        )
        return "unavailable"
    if not available:
        logger.warning("redis_readiness_unavailable")
        return "unavailable"
    return "ok"


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    """Report process liveness without accessing external dependencies."""

    return LiveResponse(status="ok", service="fitweek-api")


@router.get(
    "/ready",
    response_model=ReadyResponse,
    response_model_exclude_none=True,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
)
async def ready(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReadyResponse | JSONResponse:
    """Report the selected persistence mode without implicit fallback."""

    observability_facade = getattr(request.app.state, "observability", None)
    observability = (
        observability_facade.readiness() if observability_facade is not None else None
    )
    alerting_runtime = getattr(request.app.state, "alerting", None)
    alerting = alerting_runtime.readiness() if alerting_runtime is not None else None

    if settings.persistence_backend is PersistenceBackend.MEMORY:
        checks = {"persistence": "memory", "redis": "disabled"}
        container = getattr(request.app.state, "business_container", None)
        gateway = getattr(container, "tool_gateway", None) if container else None
        registrations = gateway.registry.registrations() if gateway else ()
        expected_tools = {
            "CALENDAR_COMMIT",
            "CALENDAR_FREE_BUSY",
            "EXERCISE_CATALOG_SEARCH",
            "ICS_EXPORT",
            "MEMORY_CANDIDATE_CREATE",
            "RECOVERY_SPACING_VALIDATOR",
            "SESSION_DURATION_CALCULATOR",
        }
        registry_ready = (
            gateway is not None
            and {item.descriptor.tool_id.value for item in registrations}
            == expected_tools
            and all(
                item.adapter is not None and item.descriptor.allowed_callers
                for item in registrations
            )
        )
        if container is not None and not registry_ready:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "not_ready",
                    "mode": "development",
                    "checks": checks,
                    "warnings": ["Tool Gateway registry is unavailable."],
                    **(
                        {"observability": observability}
                        if observability is not None
                        else {}
                    ),
                    **({"alerting": alerting} if alerting is not None else {}),
                },
            )
        tool_gateway: dict[str, object] | None = None
        if gateway is not None:
            open_circuits = await gateway.circuits.open_keys()
            degraded_tools = (
                [] if settings.calendar_write_enabled else ["CALENDAR_COMMIT:DISABLED"]
            )
            tool_gateway = {
                "status": "DEGRADED" if open_circuits else "AVAILABLE",
                "critical_tools_registered": True,
                "registered_tool_count": 7,
                "degraded_tools": degraded_tools,
                "open_circuits": open_circuits,
            }
        if settings.orchestrator_enabled:
            startup_failed = bool(
                getattr(container, "orchestrator_startup_failed", True)
            )
            pool = getattr(container, "orchestrator_pool", None)
            pool_running = pool is not None and pool.status().running
            checks["orchestrator"] = (
                "ok" if pool_running and not startup_failed else "unavailable"
            )
            if checks["orchestrator"] != "ok":
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={
                        "status": "not_ready",
                        "mode": "development",
                        "checks": checks,
                        "warnings": [
                            "Data and orchestration state are process-local "
                            "and ephemeral."
                        ],
                        **(
                            {"observability": observability}
                            if observability is not None
                            else {}
                        ),
                        **({"alerting": alerting} if alerting is not None else {}),
                    },
                )
        return ReadyResponse(
            status="ready",
            mode="development",
            checks=checks,
            tool_gateway=tool_gateway,
            observability=observability,
            alerting=alerting,
            warnings=[
                "Data is not persistent and will be lost when the process restarts."
            ],
        )

    database = getattr(request.app.state, "database", None)
    mysql = await _mysql_status(
        database if isinstance(database, Database) else get_database()
    )
    redis = (
        "disabled"
        if not settings.redis_enabled
        else await _redis_status(get_redis_manager())
    )
    checks = {"mysql": mysql, "redis": redis}

    if mysql != "ok":
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "checks": checks,
                **(
                    {"observability": observability}
                    if observability is not None
                    else {}
                ),
                **({"alerting": alerting} if alerting is not None else {}),
            },
        )

    runtime = getattr(request.app.state, "mysql_orchestration_runtime", None)
    if settings.orchestrator_enabled:
        checks["orchestrator"] = (
            "external_worker" if runtime is not None else "unavailable"
        )
        if runtime is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "checks": checks},
            )
    response_status = "degraded" if redis == "unavailable" else "ready"
    return ReadyResponse(
        status=response_status,
        checks=checks,
        observability=observability,
        alerting=alerting,
    )
