"""Persistence- and framework-independent model gateway contracts."""

from app.domain.model_gateway.enums import (
    FallbackType,
    ModelAttemptOutcome,
    ModelErrorCode,
    ProviderRole,
)
from app.domain.model_gateway.models import (
    ModelCallTrace,
    ModelGatewayResult,
    ModelProviderResponse,
    ModelRequest,
    ModelTraceContext,
)
from app.domain.model_gateway.protocols import ModelProvider

__all__ = [
    "FallbackType",
    "ModelAttemptOutcome",
    "ModelCallTrace",
    "ModelErrorCode",
    "ModelGatewayResult",
    "ModelProvider",
    "ModelProviderResponse",
    "ModelRequest",
    "ModelTraceContext",
    "ProviderRole",
]
