"""TTL, copy isolation, uniqueness, and user scope for draft storage."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.common import RepositoryUniqueError
from app.domain.profile_agent.models import ProfileAgentDraft
from app.persistence.memory.profile_agent_repository import (
    InMemoryProfileAgentDraftRepository,
)
from tests.phase3a_helpers import output_document

pytestmark = pytest.mark.phase_3a


def draft(now: datetime) -> ProfileAgentDraft:
    from app.domain.profile_agent.models import ProfileAgentOutput

    return ProfileAgentDraft(
        id=uuid4(),
        request_id=uuid4(),
        client_request_id="client-1",
        user_id=uuid4(),
        request_payload_fingerprint="a" * 64,
        input_fingerprint="b" * 64,
        output=ProfileAgentOutput.model_validate(output_document()),
        prompt_version="profile-agent-v1",
        provider_summary="fake:v1:model",
        fallback_used=False,
        fallback_type=None,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )


@pytest.mark.asyncio
async def test_save_read_user_scope_copy_and_reset() -> None:
    now = datetime(2026, 7, 13, tzinfo=UTC)
    repository = InMemoryProfileAgentDraftRepository(clock=lambda: now)
    value = draft(now)
    saved = await repository.save(value)

    assert saved == value
    assert await repository.get(value.id, value.user_id) == value
    assert await repository.get(value.id, uuid4()) is None
    assert await repository.get_by_client_request_id(value.user_id, "client-1") == value
    await repository.reset()
    assert await repository.get(value.id, value.user_id) is None


@pytest.mark.asyncio
async def test_unique_request_and_ttl_expiration() -> None:
    current = [datetime(2026, 7, 13, tzinfo=UTC)]
    repository = InMemoryProfileAgentDraftRepository(clock=lambda: current[0])
    value = draft(current[0])
    await repository.save(value)
    duplicate = replace(value, id=uuid4(), request_id=uuid4())
    with pytest.raises(RepositoryUniqueError):
        await repository.save(duplicate)

    current[0] = value.expires_at
    assert await repository.get(value.id, value.user_id) is None
    assert (
        await repository.get_by_client_request_id(
            value.user_id, value.client_request_id
        )
        is None
    )
