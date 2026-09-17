"""HTTP boundary instrumentation with route-template cardinality control."""

from __future__ import annotations

from time import perf_counter
from uuid import UUID, uuid4

from fastapi import Request, Response
from opentelemetry.trace import SpanKind
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.routing import BaseRoute, Match

from app.observability.context import (
    ObservabilityContext,
    reset_observability_context,
    set_observability_context,
)
from app.observability.facade import ObservabilityFacade


def _correlation_id(request: Request) -> UUID:
    candidate = request.headers.get("x-correlation-id")
    if candidate:
        try:
            return UUID(candidate)
        except ValueError:
            pass
    return uuid4()


def _route_template(request: Request) -> str:
    scope = dict(request.scope)
    for route in request.app.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return str(getattr(route, "path", "unmatched"))
    return "unmatched"


def _full_route_template(
    routes: list[BaseRoute],
    endpoint: object,
    prefix: str = "",
) -> str | None:
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            include_context = getattr(route, "include_context", None)
            included_prefix = str(getattr(include_context, "prefix", ""))
            result = _full_route_template(
                list(original_router.routes),
                endpoint,
                prefix + included_prefix,
            )
            if result is not None:
                return result
        elif getattr(route, "endpoint", None) is endpoint:
            return prefix + str(getattr(route, "path", "unmatched"))
    return None


def _status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


def _agent_type(path: str) -> str | None:
    if path == "/api/v1/profile-agent/parse":
        return "PROFILE_AGENT"
    if path == "/api/v1/session-designs":
        return "SESSION_DESIGNER"
    if path == "/api/v1/schedule-drafts":
        return "SCHEDULE_AGENT"
    if path == "/api/v1/recovery-drafts":
        return "RECOVERY_AGENT"
    return None


_BUSINESS_METRICS = (
    ("/plans/generate", "fitweek_plan_generations_total"),
    ("/confirm", "fitweek_plan_confirmations_total"),
    ("/schedule-drafts", "fitweek_schedule_drafts_total"),
    ("/calendar-operations", "fitweek_calendar_operations_total"),
    ("/ics-export", "fitweek_ics_exports_total"),
    ("/check-ins", "fitweek_checkins_total"),
    ("/recovery-drafts", "fitweek_recovery_drafts_total"),
    ("/recovery-applications", "fitweek_recovery_applications_total"),
    ("/revisions", "fitweek_plan_revisions_total"),
)


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Create one root span and safe HTTP metrics/logs per request."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        facade = getattr(request.app.state, "observability", None)
        if not isinstance(facade, ObservabilityFacade) or not facade.enabled:
            return await call_next(request)

        route = _route_template(request)
        method = request.method.upper()
        correlation_id = _correlation_id(request)
        context = ObservabilityContext(
            correlation_id=correlation_id,
            request_id=request.headers.get("x-request-id"),
            operation_name="http.request",
            component="api",
        )
        token = set_observability_context(context)
        labels = {"method": method, "route": route}
        facade.record_gauge_delta(
            "http_server_requests_in_progress",
            1,
            labels=labels,
        )
        started = perf_counter()
        status_code = 500
        try:
            with facade.start_span(
                "http.request",
                context=context,
                attributes={
                    "http_method": method,
                    "http_route_template": route,
                },
                kind=SpanKind.SERVER,
            ) as root_span:
                with facade.start_span(
                    "application.operation",
                    context=context.model_copy(
                        update={
                            "operation_name": "application.operation",
                            "component": "application",
                        }
                    ),
                ) as application_span:
                    agent_type = _agent_type(request.url.path)
                    if agent_type is None:
                        response = await call_next(request)
                        agent_span = None
                    else:
                        with facade.start_span(
                            "agent.invocation",
                            context=context.model_copy(
                                update={
                                    "operation_name": "agent.invoke",
                                    "component": "agent",
                                }
                            ),
                            attributes={"agent_type": agent_type},
                        ) as agent_span:
                            response = await call_next(request)
                            if response.status_code >= 500 or response.status_code in {
                                401,
                                403,
                            }:
                                agent_span.fail(
                                    "AGENT_FAILURE", f"HTTP_{response.status_code}"
                                )
                            elif response.status_code >= 400:
                                agent_span.reject(
                                    "REJECTED", f"HTTP_{response.status_code}"
                                )
                            else:
                                agent_span.succeed()
                    status_code = response.status_code
                    matched_route = request.scope.get("route")
                    resolved_route = _full_route_template(
                        list(request.app.routes),
                        getattr(matched_route, "endpoint", None),
                    ) or str(getattr(matched_route, "path", route))
                    outcome = "SUCCEEDED" if status_code < 400 else "REJECTED"
                    if status_code >= 500 or status_code in {401, 403}:
                        root_span.fail("HTTP_ERROR", f"HTTP_{status_code}")
                        application_span.fail("HTTP_ERROR", f"HTTP_{status_code}")
                        outcome = "FAILED"
                    elif status_code >= 400:
                        root_span.reject(outcome, f"HTTP_{status_code}")
                        application_span.reject(outcome, f"HTTP_{status_code}")
                    else:
                        root_span.succeed()
                        application_span.succeed()
                    root_span.set_attributes(
                        {
                            "http_status_code": status_code,
                            "http_route_template": resolved_route,
                        }
                    )
                    duration = perf_counter() - started
                    metric_labels = {
                        "method": method,
                        "route": resolved_route,
                        "status_class": _status_class(status_code),
                    }
                    facade.record_counter(
                        "http_server_requests_total",
                        labels=metric_labels,
                    )
                    facade.record_histogram(
                        "http_server_request_duration_seconds",
                        duration,
                        labels=metric_labels,
                    )
                    for marker, metric in _BUSINESS_METRICS:
                        if marker in resolved_route and method in {
                            "POST",
                            "PUT",
                            "PATCH",
                        }:
                            facade.record_counter(metric, labels={"outcome": outcome})
                            break
                    if agent_type is not None:
                        agent_labels = {
                            "agent_type": agent_type,
                            "outcome": outcome,
                        }
                        facade.record_counter(
                            "fitweek_agent_runs_total", labels=agent_labels
                        )
                        facade.record_histogram(
                            "fitweek_agent_run_duration_seconds",
                            duration,
                            labels=agent_labels,
                        )
                    facade.emit_log(
                        event_name="http_request_completed",
                        context=context,
                        level="ERROR" if outcome == "FAILED" else "INFO",
                        outcome=outcome,
                        error_category=("HTTP_ERROR" if outcome == "FAILED" else None),
                        error_code=(
                            f"HTTP_{status_code}" if status_code >= 400 else None
                        ),
                        duration_ms=duration * 1000,
                        span=root_span,
                    )
                    response.headers["x-correlation-id"] = str(correlation_id)
                    return response
        finally:
            facade.record_gauge_delta(
                "http_server_requests_in_progress",
                -1,
                labels=labels,
            )
            reset_observability_context(token)
