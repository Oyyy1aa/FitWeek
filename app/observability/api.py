"""Internal Prometheus exposition endpoint."""

from fastapi import APIRouter, Request, status
from fastapi.responses import PlainTextResponse

from app.observability.facade import ObservabilityFacade

router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> PlainTextResponse:
    facade = getattr(request.app.state, "observability", None)
    if not isinstance(facade, ObservabilityFacade) or not facade.metrics.enabled:
        return PlainTextResponse(
            "metrics disabled\n",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return PlainTextResponse(
        facade.metrics_text().decode("utf-8"),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
