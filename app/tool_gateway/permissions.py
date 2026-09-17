"""Permission check performed before adapter invocation."""

from app.domain.tools.errors import ToolPermissionDenied
from app.domain.tools.models import ToolDescriptor, ToolInvocationContext


def authorize(context: ToolInvocationContext, descriptor: ToolDescriptor) -> None:
    if context.caller not in descriptor.allowed_callers:
        raise ToolPermissionDenied()
    if descriptor.requires_idempotency_key and not context.idempotency_key:
        raise ValueError("TOOL_IDEMPOTENCY_KEY_REQUIRED")
