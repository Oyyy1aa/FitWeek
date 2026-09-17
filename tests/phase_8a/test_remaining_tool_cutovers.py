"""Formal production-path evidence for the four remaining Phase 8A tools."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import BusinessContainer
from app.config import get_settings
from app.domain.memory.enums import MemoryCandidateStatus, MemorySource, MemoryType
from app.domain.profile_agent.memory_candidates import (
    ImportDraftMemoryCandidatesCommand,
)
from app.domain.tools.enums import ToolCaller, ToolId, ToolInvocationStatus
from app.domain.tools.models import ToolInvocationContext
from app.main import create_application
from app.memory.candidate_service import CreateCandidateCommand
from app.persistence.memory.profile_agent_repository import (
    InMemoryProfileAgentDraftRepository,
)
from app.tool_adapters.basic import (
    CatalogSearchAdapter,
    MemoryCandidateCreateAdapter,
    RecoverySpacingAdapter,
    SessionDurationAdapter,
)
from app.tool_adapters.contracts import (
    ExerciseCatalogSearchRequest,
    ExerciseCatalogSearchResponse,
    MemoryCandidateCreateRequest,
    MemoryCandidateCreateResponse,
    RecoverySpacingRequest,
    RecoverySpacingResponse,
    SessionDurationRequest,
    SessionDurationResponse,
)
from tests.api.helpers import generation_payload
from tests.phase4b_helpers import draft_with_candidates

pytestmark = pytest.mark.phase_8a


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("MODEL_GATEWAY_ENABLED", "false")
    monkeypatch.setenv("CONTEXT_DEBUG_API_ENABLED", "true")
    get_settings.cache_clear()
    with TestClient(create_application()) as value:
        yield value
    get_settings.cache_clear()


def _container(client: TestClient) -> BusinessContainer:
    value = client.app.state.business_container
    assert isinstance(value, BusinessContainer)
    return value


def _profile(client: TestClient, *, maximum: int = 60) -> None:
    response = client.put(
        "/api/v1/profiles/me",
        json={
            "experience_level": "BEGINNER",
            "weekly_frequency": 2,
            "max_session_minutes": maximum,
            "primary_goal": "GENERAL_FITNESS",
            "scope_confirmed": True,
        },
    )
    assert response.status_code == 200, response.text


def _adapter(client: TestClient, tool_id: ToolId) -> object:
    return (
        _container(client)
        .tool_gateway.registry.get(tool_id.value, "phase-8a-v1")
        .adapter
    )


def _traces(client: TestClient, tool_id: ToolId):
    value = _container(client)
    return tuple(
        item
        for item in value.tool_gateway.traces.list_for_user(value.development_user.id)
        if item.tool_id is tool_id
    )


def _session_payload(request_id: str, duration: int = 30) -> dict[str, object]:
    return {
        "client_request_id": request_id,
        "target_date": "2026-07-24",
        "target_duration_minutes": duration,
        "location": "HOME",
        "goal": "GENERAL_FITNESS",
    }


def _confirmed_plan(client: TestClient) -> dict[str, object]:
    _profile(client)
    generated = client.post("/api/v1/plans/generate", json=_future_generation_payload())
    assert generated.status_code == 201, generated.text
    plan = generated.json()["plan"]
    confirmed = client.post(
        f"/api/v1/plans/{plan['id']}/confirm",
        json={"expected_version": plan["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _future_generation_payload() -> dict[str, object]:
    today = datetime.now(UTC).date()
    week_start = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    availability = []
    for offset in (0, 2):
        start = datetime.combine(
            week_start + timedelta(days=offset),
            datetime.min.time(),
            tzinfo=UTC,
        ).replace(hour=10)
        availability.append(
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "location_type": "HOME",
            }
        )
    return {
        "week_start": week_start.isoformat(),
        "availability_slots": availability,
        "preferred_locations": ["HOME"],
        "preferred_session_types": [],
    }


def _recovery_payload(plan: dict[str, object], request_id: str) -> dict[str, object]:
    sessions = plan["sessions"]
    assert isinstance(sessions, list)
    return {
        "client_request_id": request_id,
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "request_type": "RESCHEDULE_REQUEST",
        "target_session_ids": [sessions[-1]["id"]],
        "user_request": "Move one future workout to another available time.",
    }


def _candidate_payload(request_id: str, value: str = "home") -> dict[str, object]:
    return {
        "client_request_id": request_id,
        "memory_type": "PREFERRED_LOCATION",
        "key": "preferred_location",
        "value": value,
        "source": "PROFILE_AGENT_CANDIDATE",
        "source_reference": "draft:phase-8a-cutover",
        "evidence_summary": "Explicit structured candidate evidence.",
        "confidence": "0.8",
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }


def test_plan_generation_formal_path_uses_catalog_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(client)
    container = _container(client)
    original = container.exercise_repository.list_active
    repository_calls = 0

    async def counted(location=None, equipment=None):
        nonlocal repository_calls
        repository_calls += 1
        return await original(location, equipment)

    monkeypatch.setattr(container.exercise_repository, "list_active", counted)
    adapter = _adapter(client, ToolId.EXERCISE_CATALOG_SEARCH)
    before = adapter.invocation_count
    response = client.post("/api/v1/plans/generate", json=generation_payload())
    assert response.status_code == 201, response.text
    assert adapter.invocation_count - before == 1
    assert repository_calls == 1
    trace = _traces(client, ToolId.EXERCISE_CATALOG_SEARCH)[-1]
    assert trace.caller == ToolCaller.PLAN_GENERATION_APPLICATION.value
    assert trace.tool_version == "phase-8a-v1"
    assert trace.attempt_no == 1


def test_plan_generation_keeps_stable_exercise_order(client: TestClient) -> None:
    _profile(client)
    first = client.post("/api/v1/plans/generate", json=generation_payload()).json()[
        "plan"
    ]
    second = client.post("/api/v1/plans/generate", json=generation_payload()).json()[
        "plan"
    ]
    first_ids = [
        item["exercise_id"]
        for session in first["sessions"]
        for item in session["exercises"]
    ]
    second_ids = [
        item["exercise_id"]
        for session in second["sessions"]
        for item in session["exercises"]
    ]
    assert first_ids == second_ids


@pytest.mark.parametrize("feature", ["jumping", "running"])
def test_plan_catalog_keeps_excluded_feature_filter(
    client: TestClient, feature: str
) -> None:
    _profile(client)
    created = client.post(
        "/api/v1/profiles/me/constraints",
        json={
            "constraint_type": "EXCLUDED_FEATURE",
            "constraint_value": feature,
            "priority": 100,
            "is_hard": True,
            "source": "USER_EXPLICIT",
        },
    )
    assert created.status_code == 201
    plan = client.post("/api/v1/plans/generate", json=generation_payload()).json()[
        "plan"
    ]
    catalog = {
        item.id: item
        for item in _container(client).store._exercises.values()  # noqa: SLF001
    }
    selected = {
        item["exercise_id"]
        for session in plan["sessions"]
        for item in session["exercises"]
    }
    assert all(feature not in catalog[item].feature_tags for item in selected)


def test_plan_catalog_keeps_home_location_filter(client: TestClient) -> None:
    _profile(client)
    plan = client.post("/api/v1/plans/generate", json=generation_payload()).json()[
        "plan"
    ]
    catalog = _container(client).store._exercises  # noqa: SLF001
    selected = {
        item["exercise_id"]
        for session in plan["sessions"]
        for item in session["exercises"]
    }
    assert all(
        "HOME" in {x.value for x in catalog[item].location_types} for item in selected
    )


def test_plan_catalog_keeps_beginner_difficulty_filter(client: TestClient) -> None:
    _profile(client)
    plan = client.post("/api/v1/plans/generate", json=generation_payload()).json()[
        "plan"
    ]
    catalog = _container(client).store._exercises  # noqa: SLF001
    selected = {
        item["exercise_id"]
        for session in plan["sessions"]
        for item in session["exercises"]
    }
    assert all(catalog[item].difficulty_level.value != "ADVANCED" for item in selected)


def test_empty_catalog_is_one_normal_tool_attempt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(client)
    repository = _container(client).exercise_repository
    calls = 0

    async def empty(location=None, equipment=None):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(repository, "list_active", empty)
    response = client.post("/api/v1/plans/generate", json=generation_payload())
    assert response.status_code == 422
    assert calls == 1
    trace = _traces(client, ToolId.EXERCISE_CATALOG_SEARCH)[-1]
    assert trace.attempt_no == 1
    assert trace.status == "SUCCEEDED"


def test_session_design_formal_path_uses_catalog_and_duration(
    client: TestClient,
) -> None:
    _profile(client)
    catalog = _adapter(client, ToolId.EXERCISE_CATALOG_SEARCH)
    duration = _adapter(client, ToolId.SESSION_DURATION_CALCULATOR)
    catalog_before = catalog.invocation_count
    duration_before = duration.invocation_count
    response = client.post(
        "/api/v1/session-designs", json=_session_payload("formal-session")
    )
    assert response.status_code == 201, response.text
    assert catalog.invocation_count - catalog_before == 1
    assert duration.invocation_count - duration_before == 1
    assert _traces(client, ToolId.EXERCISE_CATALOG_SEARCH)[-1].caller == (
        ToolCaller.SESSION_DESIGN_APPLICATION.value
    )
    assert _traces(client, ToolId.SESSION_DURATION_CALCULATOR)[-1].caller == (
        ToolCaller.SESSION_DESIGN_APPLICATION.value
    )


@pytest.mark.parametrize("minutes", [15, 30, 45, 60])
def test_duration_cutover_preserves_exact_supported_targets(
    client: TestClient, minutes: int
) -> None:
    _profile(client, maximum=60)
    response = client.post(
        "/api/v1/session-designs",
        json=_session_payload(f"duration-{minutes}", minutes),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["total_seconds"] == minutes * 60
    assert body["duration_policy_version"] == "session-duration-policy-v1"
    trace = _traces(client, ToolId.SESSION_DURATION_CALCULATOR)[-1]
    assert trace.attempt_no == 1 and trace.status == "SUCCEEDED"


def test_session_candidate_membership_remains_sorted(client: TestClient) -> None:
    _profile(client)
    response = client.post(
        "/api/v1/session-designs", json=_session_payload("candidate-order")
    )
    assert response.status_code == 201
    body = response.json()
    candidate_set = _container(client).store._session_candidate_sets[  # noqa: SLF001
        UUID(body["candidate_set_id"])
    ]
    assert all(
        slot.exercise_ids == tuple(sorted(slot.exercise_ids))
        for slot in candidate_set.slots
    )


def test_session_catalog_and_candidate_fingerprints_are_stable(
    client: TestClient,
) -> None:
    _profile(client)
    payload = _session_payload("same-session")
    first = client.post("/api/v1/session-designs", json=payload)
    second = client.post("/api/v1/session-designs", json=payload)
    assert first.status_code == 201 and second.status_code == 200
    assert (
        first.json()["candidate_set_fingerprint"]
        == second.json()["candidate_set_fingerprint"]
    )
    assert first.json()["catalog_version"] == second.json()["catalog_version"]


def test_session_empty_catalog_stops_before_duration(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(client)

    async def empty(location=None, equipment=None):
        return []

    monkeypatch.setattr(_container(client).exercise_repository, "list_active", empty)
    duration = _adapter(client, ToolId.SESSION_DURATION_CALCULATOR)
    before = duration.invocation_count
    response = client.post(
        "/api/v1/session-designs", json=_session_payload("empty-session")
    )
    assert response.status_code == 422
    assert duration.invocation_count == before
    assert _traces(client, ToolId.EXERCISE_CATALOG_SEARCH)[-1].attempt_no == 1


def test_recovery_formal_path_uses_spacing_once(client: TestClient) -> None:
    plan = _confirmed_plan(client)
    adapter = _adapter(client, ToolId.RECOVERY_SPACING_VALIDATOR)
    before = adapter.invocation_count
    response = client.post(
        "/api/v1/recovery-drafts",
        json=_recovery_payload(plan, "recovery-spacing-formal"),
    )
    assert response.status_code == 201, response.text
    assert adapter.invocation_count - before == 1
    trace = _traces(client, ToolId.RECOVERY_SPACING_VALIDATOR)[-1]
    assert trace.caller == ToolCaller.RECOVERY_APPLICATION.value
    assert trace.tool_version == "phase-8a-v1"
    assert trace.attempt_no == 1


@pytest.mark.parametrize(
    "request_type",
    ["RESCHEDULE_REQUEST", "REDUCE_FUTURE_LOAD", "GENERAL_RECOVERY_REVIEW"],
)
def test_recovery_spacing_cutover_preserves_controlled_outcomes(
    client: TestClient, request_type: str
) -> None:
    plan = _confirmed_plan(client)
    payload = _recovery_payload(plan, f"recovery-{request_type}")
    payload["request_type"] = request_type
    if request_type == "GENERAL_RECOVERY_REVIEW":
        payload["target_session_ids"] = None
    response = client.post("/api/v1/recovery-drafts", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["source"] == "DETERMINISTIC_FALLBACK"
    trace = _traces(client, ToolId.RECOVERY_SPACING_VALIDATOR)[-1]
    assert trace.status == "SUCCEEDED" and trace.attempt_no == 1


def test_memory_candidate_formal_api_uses_internal_write_tool(
    client: TestClient,
) -> None:
    adapter = _adapter(client, ToolId.MEMORY_CANDIDATE_CREATE)
    before = adapter.invocation_count
    response = client.post(
        "/api/v1/memory-candidates", json=_candidate_payload("memory-formal")
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == MemoryCandidateStatus.PENDING_REVIEW.value
    assert adapter.invocation_count - before == 1
    trace = _traces(client, ToolId.MEMORY_CANDIDATE_CREATE)[-1]
    assert trace.caller == ToolCaller.MEMORY_COMMITTER.value
    assert trace.tool_version == "phase-8a-v1"
    assert trace.attempt_no == 1


def test_pending_memory_candidate_is_not_recalled(client: TestClient) -> None:
    _profile(client)
    candidate = client.post(
        "/api/v1/memory-candidates", json=_candidate_payload("memory-pending")
    ).json()
    context = client.post(
        "/api/v1/contexts/build",
        json={"agent_type": "PROFILE_AGENT", "current_task": {"type": "profile"}},
    )
    assert context.status_code == 200
    assert candidate["id"] not in context.text


def test_accepted_memory_candidate_is_recalled(client: TestClient) -> None:
    _profile(client)
    candidate = client.post(
        "/api/v1/memory-candidates", json=_candidate_payload("memory-accepted")
    ).json()
    accepted = client.post(
        f"/api/v1/memory-candidates/{candidate['id']}/accept",
        json={
            "client_request_id": "accept-memory-cutover",
            "expected_candidate_version": 1,
            "confirmed_value": "home",
            "valid_until": None,
        },
    )
    assert accepted.status_code == 200
    context = client.post(
        "/api/v1/contexts/build",
        json={"agent_type": "PROFILE_AGENT", "current_task": {"type": "profile"}},
    )
    assert "home" in context.text


def test_memory_candidate_same_request_is_idempotently_reused(
    client: TestClient,
) -> None:
    payload = _candidate_payload("memory-idempotent")
    first = client.post("/api/v1/memory-candidates", json=payload)
    second = client.post("/api/v1/memory-candidates", json=payload)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    traces = _traces(client, ToolId.MEMORY_CANDIDATE_CREATE)
    assert traces[-1].attempt_no == 1 and traces[-1].status == "SUCCEEDED"


def test_memory_candidate_conflict_maps_to_existing_http_409(
    client: TestClient,
) -> None:
    first = _candidate_payload("memory-conflict", "home")
    changed = _candidate_payload("memory-conflict", "gym")
    assert client.post("/api/v1/memory-candidates", json=first).status_code == 201
    conflict = client.post("/api/v1/memory-candidates", json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == ("MEMORY_CANDIDATE_IDEMPOTENCY_CONFLICT")
    trace = _traces(client, ToolId.MEMORY_CANDIDATE_CREATE)[-1]
    assert trace.attempt_no == 1 and trace.status == "FAILED"


@pytest.mark.asyncio
async def test_profile_draft_import_uses_memory_committer_gateway() -> None:
    from tests.phase4b_helpers import container

    value = container()
    draft = draft_with_candidates(value)
    await InMemoryProfileAgentDraftRepository(value.store).save(draft)
    adapter = value.tool_gateway.registry.get(
        ToolId.MEMORY_CANDIDATE_CREATE.value, "phase-8a-v1"
    ).adapter
    before = adapter.invocation_count
    result = await value.profile_draft_memory_candidate_service.import_candidates(
        user=value.development_user,
        draft_id=draft.id,
        command=ImportDraftMemoryCandidatesCommand(
            client_request_id="profile-draft-memory-cutover",
            expected_draft_version=1,
            selected_candidate_indexes=(0,),
        ),
    )
    assert result.statuses == (MemoryCandidateStatus.PENDING_REVIEW,)
    assert adapter.invocation_count - before == 1
    trace = value.tool_gateway.traces.list_for_user(value.development_user.id)[-1]
    assert trace.tool_id is ToolId.MEMORY_CANDIDATE_CREATE
    assert trace.caller == ToolCaller.MEMORY_COMMITTER.value


@pytest.mark.parametrize(
    ("tool_id", "request_model", "response_model", "adapter_type"),
    [
        (
            ToolId.EXERCISE_CATALOG_SEARCH,
            ExerciseCatalogSearchRequest,
            ExerciseCatalogSearchResponse,
            CatalogSearchAdapter,
        ),
        (
            ToolId.SESSION_DURATION_CALCULATOR,
            SessionDurationRequest,
            SessionDurationResponse,
            SessionDurationAdapter,
        ),
        (
            ToolId.RECOVERY_SPACING_VALIDATOR,
            RecoverySpacingRequest,
            RecoverySpacingResponse,
            RecoverySpacingAdapter,
        ),
        (
            ToolId.MEMORY_CANDIDATE_CREATE,
            MemoryCandidateCreateRequest,
            MemoryCandidateCreateResponse,
            MemoryCandidateCreateAdapter,
        ),
    ],
)
def test_remaining_tool_descriptors_are_typed_single_attempt(
    client: TestClient,
    tool_id: ToolId,
    request_model: object,
    response_model: object,
    adapter_type: object,
) -> None:
    registration = _container(client).tool_gateway.registry.get(
        tool_id.value, "phase-8a-v1"
    )
    assert registration.descriptor.request_model is request_model
    assert registration.descriptor.response_model is response_model
    assert registration.descriptor.max_attempts == 1
    assert registration.descriptor.circuit_breaker_enabled is False
    assert isinstance(registration.adapter, adapter_type)


@pytest.mark.asyncio
async def test_catalog_permission_rejection_does_not_call_adapter(
    client: TestClient,
) -> None:
    value = _container(client)
    adapter = _adapter(client, ToolId.EXERCISE_CATALOG_SEARCH)
    before = adapter.invocation_count
    now = value.tool_gateway.clock.now()
    result = await value.tool_gateway.invoke(
        ToolInvocationContext(
            invocation_id=uuid4(),
            correlation_id=uuid4(),
            user_id=value.development_user.id,
            caller=ToolCaller.PROFILE_APPLICATION,
            tool_id=ToolId.EXERCISE_CATALOG_SEARCH,
            tool_version="phase-8a-v1",
            deadline_at=now + timedelta(seconds=1),
            created_at=now,
        ),
        ExerciseCatalogSearchRequest(user_id=value.development_user.id),
    )
    assert result.result.status is ToolInvocationStatus.REJECTED
    assert adapter.invocation_count == before


@pytest.mark.asyncio
async def test_memory_permission_rejection_does_not_call_adapter(
    client: TestClient,
) -> None:
    value = _container(client)
    adapter = _adapter(client, ToolId.MEMORY_CANDIDATE_CREATE)
    before = adapter.invocation_count
    now = value.tool_gateway.clock.now()
    command = CreateCandidateCommand(
        client_request_id="illegal-memory-caller",
        memory_type=MemoryType.PREFERRED_LOCATION,
        key="preferred_location",
        value="home",
        source=MemorySource.PROFILE_AGENT_CANDIDATE,
        source_reference="draft:illegal",
        evidence_summary="Controlled evidence.",
        confidence=Decimal("0.8"),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    result = await value.tool_gateway.invoke(
        ToolInvocationContext(
            invocation_id=uuid4(),
            correlation_id=uuid4(),
            user_id=value.development_user.id,
            caller=ToolCaller.PROFILE_APPLICATION,
            tool_id=ToolId.MEMORY_CANDIDATE_CREATE,
            tool_version="phase-8a-v1",
            deadline_at=now + timedelta(seconds=1),
            created_at=now,
            idempotency_key=command.client_request_id,
        ),
        MemoryCandidateCreateRequest(
            user_id=value.development_user.id, command=command
        ),
    )
    assert result.result.status is ToolInvocationStatus.REJECTED
    assert adapter.invocation_count == before


def test_memory_tool_response_does_not_expose_repository_entity() -> None:
    assert "outcome" not in MemoryCandidateCreateResponse.model_fields
    assert set(MemoryCandidateCreateResponse.model_fields) == {
        "candidate_id",
        "status",
        "version",
        "idempotent_reuse",
        "result_fingerprint",
    }
