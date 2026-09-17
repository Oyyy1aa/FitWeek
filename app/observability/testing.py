"""Test-only observability sinks; none are exposed through public APIs."""

from collections.abc import Mapping


class FailingLogSink:
    def emit(self, payload: Mapping[str, object]) -> None:
        raise RuntimeError("isolated log sink failure")
