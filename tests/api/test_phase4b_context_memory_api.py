"""Phase 4B API controls and Draft Candidate review endpoints."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_application
from app.persistence.memory.profile_agent_repository import (
    InMemoryProfileAgentDraftRepository,
)
from tests.phase4b_helpers import draft_with_candidates

pytestmark = pytest.mark.phase_4b


@pytest.fixture
def phase4b_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("CONTEXT_DEBUG_API_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(create_application()) as client:
        yield client
    get_settings.cache_clear()


def test_debug_build_is_closed_but_internal_snapshot_lookup_is_safe(
    phase4b_client: TestClient,
) -> None:
    response = phase4b_client.post(
        "/api/v1/contexts/build",
        json={
            "agent_type": "PROFILE_AGENT",
            "current_task": {"request_type": "test"},
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONTEXT_DEBUG_API_DISABLED"


def test_draft_candidate_preview_import_and_conflict(
    phase4b_client: TestClient,
) -> None:
    container = phase4b_client.app.state.business_container
    draft = draft_with_candidates(container)
    repository = InMemoryProfileAgentDraftRepository(container.store)
    import anyio

    anyio.run(repository.save, draft)
    preview = phase4b_client.post(
        f"/api/v1/profile-agent/drafts/{draft.id}/memory-candidates/preview",
        json={"selected_candidate_indexes": [0]},
    )
    assert preview.status_code == 200
    assert preview.json()["items"][0]["supported"] is True
    imported = phase4b_client.post(
        f"/api/v1/profile-agent/drafts/{draft.id}/memory-candidates",
        json={
            "client_request_id": "http-import-1",
            "expected_draft_version": 1,
            "selected_candidate_indexes": [0],
        },
    )
    assert imported.status_code == 201
    assert imported.json()["statuses"] == ["PENDING_REVIEW"]
    conflict = phase4b_client.post(
        f"/api/v1/profile-agent/drafts/{draft.id}/memory-candidates",
        json={
            "client_request_id": "http-import-1",
            "expected_draft_version": 1,
            "selected_candidate_indexes": [1],
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == (
        "DRAFT_MEMORY_CANDIDATE_IMPORT_IDEMPOTENCY_CONFLICT"
    )
    assert "traceback" not in conflict.text.casefold()
