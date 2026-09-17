"""Exporter protocols and failure-isolation test doubles."""

from collections.abc import Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


class FailingSpanExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise TimeoutError("isolated exporter timeout")

    def shutdown(self) -> None:
        return None
