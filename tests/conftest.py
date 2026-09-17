"""Shared test isolation fixtures."""

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import get_settings
from app.infrastructure.redis_client import get_redis_manager
from app.persistence.database import get_database

_LEGACY_PHASE_CLOCK = datetime(2026, 7, 19, 12, tzinfo=UTC)
_ENVIRONMENT_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_INTEGRATION_ENVIRONMENT_KEYS = {"TEST_DATABASE_URL", "TEST_REDIS_URL"}


def _load_integration_environment() -> None:
    """Load only integration URLs from the ignored local .env file."""

    environment_file = Path(__file__).resolve().parents[1] / ".env"
    if not environment_file.is_file():
        return
    for line in environment_file.read_text(encoding="utf-8").splitlines():
        match = _ENVIRONMENT_LINE.match(line.strip())
        if match is None or match.group(1) not in _INTEGRATION_ENVIRONMENT_KEYS:
            continue
        os.environ.setdefault(match.group(1), match.group(2))


_load_integration_environment()


@pytest.fixture(autouse=True)
def freeze_legacy_phase_acceptance_clock(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep date-sensitive Phase 5B/6 tests repeatable after their fixture week."""

    if not any(
        request.node.get_closest_marker(name)
        for name in ("phase_5b", "phase_6a", "phase_6b", "phase_7a", "phase_7b")
    ):
        return
    monkeypatch.setattr(
        "app.orchestration.clock.SystemClock.now",
        lambda _clock: _LEGACY_PHASE_CLOCK,
    )
    monkeypatch.setattr(
        "app.application.session_design_application.utc_now",
        lambda: _LEGACY_PHASE_CLOCK,
    )
    monkeypatch.setattr(
        "app.application.session_designs.utc_now",
        lambda: _LEGACY_PHASE_CLOCK,
    )
    monkeypatch.setattr(
        "app.application.recovery_applications.utc_now",
        lambda: _LEGACY_PHASE_CLOCK,
    )
    monkeypatch.setattr(
        "app.memory.candidate_service.utc_now",
        lambda: _LEGACY_PHASE_CLOCK,
    )


@pytest.fixture(autouse=True)
def clear_process_caches() -> Iterator[None]:
    """Keep cached settings and database boundaries isolated between tests."""

    get_settings.cache_clear()
    get_database.cache_clear()
    get_redis_manager.cache_clear()
    yield
    get_redis_manager.cache_clear()
    get_database.cache_clear()
    get_settings.cache_clear()
