"""Phase 8B1 process-local application fixtures."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_application


@pytest.fixture
def observability_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER", "in_memory")
    monkeypatch.setenv("PROMETHEUS_METRICS_ENABLED", "true")
    monkeypatch.setenv("STRUCTURED_LOGGING_ENABLED", "true")
    with TestClient(create_application()) as client:
        yield client
