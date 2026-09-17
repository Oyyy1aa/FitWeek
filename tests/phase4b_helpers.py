"""Focused fixtures for Phase 4B memory-aware integration tests."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from app.api.dependencies import BusinessContainer, build_memory_container
from app.application.profiles import UpsertProfileCommand
from app.config import Settings
from app.domain.memory.enums import MemoryType
from app.domain.memory.models import MemoryWriteOutcome
from app.domain.profile_agent.models import (
    ProfileAgentDraft,
    ProfileAgentOutput,
    ProfileDraftStatus,
)
from app.domain.profiles.models import ExperienceLevel, FitnessGoal
from app.memory.service import CreateMemoryCommand
from tests.phase3a_helpers import output_document


def container(*, orchestrator: bool = False) -> BusinessContainer:
    return build_memory_container(
        Settings(
            persistence_backend="memory",
            redis_enabled=False,
            orchestrator_enabled=orchestrator,
            model_gateway_enabled=True,
            context_debug_api_enabled=False,
        )
    )


async def seed_profile(value: BusinessContainer, frequency: int = 2) -> None:
    await value.profile_service.upsert_profile(
        value.development_user,
        UpsertProfileCommand(
            experience_level=ExperienceLevel.BEGINNER,
            weekly_frequency=frequency,
            max_session_minutes=30,
            primary_goal=FitnessGoal.GENERAL_FITNESS,
            scope_confirmed=True,
        ),
    )


async def create_memory(
    value: BusinessContainer,
    *,
    memory_type: MemoryType,
    key: str,
    memory_value: str,
    request_id: str | None = None,
) -> MemoryWriteOutcome:
    return await value.memory_application_service.create_memory(
        value.development_user,
        CreateMemoryCommand(
            client_request_id=request_id or str(uuid4()),
            memory_type=memory_type,
            key=key,
            value=memory_value,
            valid_until=None,
        ),
    )


def draft_with_candidates(value: BusinessContainer) -> ProfileAgentDraft:
    now = datetime.now(UTC)
    output = output_document(category="long_term")
    output["memory_candidates"] = [
        {
            "category": "PREFERENCE",
            "value": "prefers morning training",
            "rationale": "Explicit long-term preference requiring review.",
        },
        {
            "category": "PREFERENCE",
            "value": "medical condition treatment",
            "rationale": "Unsupported medical inference.",
        },
    ]
    return ProfileAgentDraft(
        id=uuid4(),
        request_id=uuid4(),
        client_request_id=f"draft-{uuid4()}",
        user_id=value.development_user.id,
        request_payload_fingerprint="a" * 64,
        input_fingerprint="b" * 64,
        output=ProfileAgentOutput.model_validate(output),
        prompt_version="profile-agent-v2",
        provider_summary="scripted local provider",
        fallback_used=False,
        fallback_type=None,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        status=ProfileDraftStatus.PENDING_REVIEW,
        version=1,
    )


TEST_WEEK = date(2030, 1, 7)
