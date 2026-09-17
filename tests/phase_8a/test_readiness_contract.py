"""Backward-compatible readiness evidence for the registered Tool plane."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.domain.tools.enums import ToolErrorCategory
from app.main import create_application
from app.tool_gateway.circuit_breaker import CircuitKey

pytestmark = pytest.mark.phase_8a


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("CALENDAR_WRITE_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_application()) as value:
        yield value
    get_settings.cache_clear()


def test_readiness_keeps_existing_fields_and_adds_tool_summary(
    client: TestClient,
) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["mode"] == "development"
    assert payload["checks"] == {"persistence": "memory", "redis": "disabled"}
    assert payload["tool_gateway"] == {
        "status": "AVAILABLE",
        "critical_tools_registered": True,
        "registered_tool_count": 7,
        "degraded_tools": ["CALENDAR_COMMIT:DISABLED"],
        "open_circuits": [],
    }


@pytest.mark.asyncio
async def test_open_circuit_is_visible_but_readiness_remains_200(
    client: TestClient,
) -> None:
    gateway = client.app.state.business_container.tool_gateway
    key = CircuitKey("CALENDAR_FREE_BUSY", "phase-8a-v1", "calendar", "read")
    for _ in range(5):
        await gateway.circuits.record_failure(key, ToolErrorCategory.CONNECTION)
    response = client.get("/health/ready")
    assert response.status_code == 200
    summary = response.json()["tool_gateway"]
    assert summary["status"] == "DEGRADED"
    assert summary["open_circuits"] == ["CALENDAR_FREE_BUSY:phase-8a-v1:calendar:read"]


def test_missing_critical_registration_makes_readiness_503(
    client: TestClient,
) -> None:
    registry = client.app.state.business_container.tool_gateway.registry
    saved = dict(registry._registrations)  # noqa: SLF001
    registry._registrations.pop(  # noqa: SLF001
        ("CALENDAR_COMMIT", "phase-8a-v1")
    )
    try:
        response = client.get("/health/ready")
    finally:
        registry._registrations.clear()  # noqa: SLF001
        registry._registrations.update(saved)  # noqa: SLF001
    assert response.status_code == 503
    assert response.json()["warnings"] == ["Tool Gateway registry is unavailable."]
