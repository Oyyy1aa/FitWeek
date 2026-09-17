"""Fresh lifespan-owned memory application for every API test."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_application


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("CONTEXT_DEBUG_API_ENABLED", "true")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        yield client
    get_settings.cache_clear()
