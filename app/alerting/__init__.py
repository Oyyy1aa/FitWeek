"""Process-local alert evaluation, routing, and notification contracts."""

from app.alerting.runtime import AlertingRuntime, build_alerting_runtime

__all__ = ["AlertingRuntime", "build_alerting_runtime"]
