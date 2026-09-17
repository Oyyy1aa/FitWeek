"""Profile Draft proposals require import and then a second explicit accept."""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.domain.context.enums import AgentType, ContextSectionName
from app.domain.context.models import ContextBuildCommand
from app.domain.memory.enums import MemoryCandidateStatus
from app.domain.memory.errors import (
    DraftMemoryCandidateConflictError,
    DraftMemoryCandidateImportIdempotencyConflictError,
    DraftMemoryCandidateIndexInvalidError,
    DraftMemoryCandidateUnsupportedError,
)
from app.domain.profile_agent.memory_candidates import (
    ImportDraftMemoryCandidatesCommand,
)
from app.memory.candidate_service import AcceptCandidateCommand
from app.persistence.memory.profile_agent_repository import (
    InMemoryProfileAgentDraftRepository,
)
from tests.phase4b_helpers import container, draft_with_candidates, seed_profile

pytestmark = pytest.mark.phase_4b


@pytest.mark.asyncio
async def test_preview_is_read_only_and_import_is_pending_and_idempotent() -> None:
    value = container()
    await seed_profile(value)
    draft = draft_with_candidates(value)
    await InMemoryProfileAgentDraftRepository(value.store).save(draft)
    service = value.profile_draft_memory_candidate_service

    before = await value.memory_application_service.list_candidates(
        value.development_user
    )
    preview = await service.preview(
        user=value.development_user,
        draft_id=draft.id,
        selected_candidate_indexes=(0,),
    )
    assert preview.items[0].supported
    assert (
        await value.memory_application_service.list_candidates(value.development_user)
        == before
    )

    command = ImportDraftMemoryCandidatesCommand(
        client_request_id="import-1",
        expected_draft_version=1,
        selected_candidate_indexes=(0,),
    )
    first = await service.import_candidates(
        user=value.development_user, draft_id=draft.id, command=command
    )
    second = await service.import_candidates(
        user=value.development_user, draft_id=draft.id, command=command
    )
    assert first.candidate_ids == second.candidate_ids
    assert first.statuses == (MemoryCandidateStatus.PENDING_REVIEW,)
    assert (
        len(
            await value.memory_application_service.list_candidates(
                value.development_user
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_pending_candidate_then_accept_changes_only_new_context() -> None:
    value = container()
    await seed_profile(value)
    draft = draft_with_candidates(value)
    await InMemoryProfileAgentDraftRepository(value.store).save(draft)
    imported = await value.profile_draft_memory_candidate_service.import_candidates(
        user=value.development_user,
        draft_id=draft.id,
        command=ImportDraftMemoryCandidatesCommand(
            client_request_id="import-visibility",
            expected_draft_version=1,
            selected_candidate_indexes=(0,),
        ),
    )
    command = ContextBuildCommand(
        agent_type=AgentType.PROFILE_AGENT,
        current_task={"request_type": "profile_parse"},
    )
    pending = await value.context_application_service.build_snapshot(
        value.development_user, command, scope_id="before-accept"
    )
    assert not pending.reference.memory_versions

    candidate = await value.memory_application_service.get_candidate(
        value.development_user, imported.candidate_ids[0]
    )
    reviewed = await value.memory_application_service.accept_candidate(
        value.development_user,
        candidate.id,
        AcceptCandidateCommand(
            client_request_id="accept-draft-candidate",
            expected_candidate_version=candidate.version,
            confirmed_value=candidate.proposed_value,
            valid_until=None,
        ),
    )
    assert reviewed.memory is not None
    active = await value.context_application_service.build_snapshot(
        value.development_user, command, scope_id="after-accept"
    )
    memories = next(
        section
        for section in active.context.sections
        if section.name is ContextSectionName.RELEVANT_CONFIRMED_MEMORIES
    )
    assert memories.items[0].value == candidate.proposed_value
    assert not pending.reference.memory_versions


@pytest.mark.asyncio
async def test_candidate_selection_medical_and_idempotency_guards() -> None:
    value = container()
    draft = draft_with_candidates(value)
    repository = InMemoryProfileAgentDraftRepository(value.store)
    await repository.save(draft)
    service = value.profile_draft_memory_candidate_service
    with pytest.raises(DraftMemoryCandidateIndexInvalidError):
        await service.preview(
            user=value.development_user,
            draft_id=draft.id,
            selected_candidate_indexes=(10,),
        )
    with pytest.raises(DraftMemoryCandidateUnsupportedError):
        await service.import_candidates(
            user=value.development_user,
            draft_id=draft.id,
            command=ImportDraftMemoryCandidatesCommand(
                client_request_id="medical-import",
                expected_draft_version=1,
                selected_candidate_indexes=(1,),
            ),
        )
    await service.import_candidates(
        user=value.development_user,
        draft_id=draft.id,
        command=ImportDraftMemoryCandidatesCommand(
            client_request_id="conflict-import",
            expected_draft_version=1,
            selected_candidate_indexes=(0,),
        ),
    )
    with pytest.raises(DraftMemoryCandidateImportIdempotencyConflictError):
        await service.import_candidates(
            user=value.development_user,
            draft_id=draft.id,
            command=ImportDraftMemoryCandidatesCommand(
                client_request_id="conflict-import",
                expected_draft_version=1,
                selected_candidate_indexes=(1,),
            ),
        )
    other = replace(value.development_user, id=uuid4())
    with pytest.raises(DraftMemoryCandidateConflictError):
        await service.preview(
            user=other,
            draft_id=draft.id,
            selected_candidate_indexes=(0,),
        )
