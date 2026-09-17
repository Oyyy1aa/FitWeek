"""Memory-mode lifecycle and health behavior."""

import pytest
from fastapi.testclient import TestClient

import app.api.health as health_module
import app.main as main_module
from app.config import get_settings

pytestmark = pytest.mark.phase_1a


class FakeDatabase:
    async def dispose(self) -> None:
        return None


def test_memory_live_and_ready_do_not_touch_mysql_or_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden() -> None:
        raise AssertionError("external infrastructure must not be initialized")

    monkeypatch.setattr(main_module, "get_database", forbidden)
    monkeypatch.setattr(main_module, "get_redis_manager", forbidden)
    monkeypatch.setattr(health_module, "get_database", forbidden)
    monkeypatch.setattr(health_module, "get_redis_manager", forbidden)
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    get_settings.cache_clear()

    with TestClient(main_module.create_application()) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
    get_settings.cache_clear()

    assert live.status_code == 200
    assert live.json() == {"status": "ok", "service": "fitweek-api"}
    assert ready.status_code == 200
    payload = ready.json()
    assert payload["status"] == "ready"
    assert payload["mode"] == "development"
    assert payload["checks"] == {"persistence": "memory", "redis": "disabled"}
    assert payload["warnings"] == [
        "Data is not persistent and will be lost when the process restarts."
    ]
    assert payload["tool_gateway"] == {
        "status": "AVAILABLE",
        "critical_tools_registered": True,
        "registered_tool_count": 7,
        "degraded_tools": ["CALENDAR_COMMIT:DISABLED"],
        "open_circuits": [],
    }


def test_mysql_backend_does_not_silently_fall_back_to_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "mysql")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setattr(main_module, "get_database", FakeDatabase)
    get_settings.cache_clear()

    with TestClient(main_module.create_application()) as client:
        response = client.get("/api/v1/profiles/me")
    get_settings.cache_clear()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "UNSUPPORTED_PERSISTENCE_BACKEND"
    assert "traceback" not in response.text.lower()
