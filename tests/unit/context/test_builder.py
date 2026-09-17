"""Context contracts, priority, isolation, budgets, Audit, and NO_MEMORY."""

from datetime import timedelta
from uuid import uuid4

import pytest

from app.domain.context.enums import AgentType, ContextDegradedMode, ContextSectionName
from app.domain.context.models import ContextBuildCommand
from app.domain.memory.enums import MemoryType
from app.domain.memory.errors import ContextBudgetExceededError, MemoryNotFoundError
from app.domain.profiles.models import ConstraintType
from app.memory.service import CreateMemoryCommand, ReplaceMemoryCommand
from app.persistence.memory.memory_repository import InMemoryMemoryRepository
from tests.phase4a_helpers import add_constraint, future, memory_container, seed_profile

pytestmark = pytest.mark.phase_4a


def _items(context: object, section_name: ContextSectionName) -> dict[str, str]:
    sections = context.sections
    section = next(item for item in sections if item.name is section_name)
    return {item.key: item.value for item in section.items}


@pytest.mark.asyncio
async def test_contracts_include_only_relevant_active_confirmed_memory() -> None:
    container = memory_container()
    await seed_profile(container)
    memory_service = container.memory_application_service
    location = await memory_service.create_memory(
        container.development_user,
        CreateMemoryCommand(
            client_request_id="ctx-location",
            memory_type=MemoryType.PREFERRED_LOCATION,
            key="preferred_location",
            value="home",
            valid_until=None,
        ),
    )
    style = await memory_service.create_memory(
        container.development_user,
        CreateMemoryCommand(
            client_request_id="ctx-style",
            memory_type=MemoryType.TRAINING_STYLE_PREFERENCE,
            key="training_style",
            value="circuit",
            valid_until=None,
        ),
    )
    profile_context = await container.context_application_service.build(
        container.development_user,
        ContextBuildCommand(
            agent_type=AgentType.PROFILE_AGENT,
            current_task={"request_type": "profile_update"},
        ),
    )
    profile_memories = _items(
        profile_context, ContextSectionName.RELEVANT_CONFIRMED_MEMORIES
    )
    assert profile_memories == {"preferred_location": "home"}
    audit = await container.context_application_service.get_audit(
        container.development_user, profile_context.audit_id
    )
    assert audit.included_memory_ids == (location.record.memory.id,)
    assert style.record.memory.id in audit.excluded_memory_ids
    assert all(
        "trace" not in item.value.casefold()
        for section in profile_context.sections
        for item in section.items
    )


@pytest.mark.asyncio
async def test_current_task_and_hard_constraint_shadow_memory() -> None:
    container = memory_container()
    await seed_profile(container)
    await add_constraint(container, ConstraintType.ALLOWED_LOCATION, "GYM")
    await add_constraint(container, ConstraintType.EXCLUDED_FEATURE, "running")
    service = container.memory_application_service
    for request_id, memory_type, key, value in (
        ("home", MemoryType.PREFERRED_LOCATION, "preferred_location", "HOME"),
        ("run", MemoryType.TRAINING_STYLE_PREFERENCE, "training_style", "running"),
        ("time", MemoryType.PREFERRED_TIME_OF_DAY, "preferred_time_of_day", "morning"),
    ):
        await service.create_memory(
            container.development_user,
            CreateMemoryCommand(
                client_request_id=request_id,
                memory_type=memory_type,
                key=key,
                value=value,
                valid_until=None,
            ),
        )
    context = await container.context_application_service.build(
        container.development_user,
        ContextBuildCommand(
            agent_type=AgentType.PLAN_GENERATION,
            current_task={"preferred_time_of_day": "evening"},
            catalog_reference="catalog-v1",
        ),
    )
    assert not any(
        section.name is ContextSectionName.RELEVANT_CONFIRMED_MEMORIES
        for section in context.sections
    )
    sources = {item.higher_priority_source for item in context.conflicts}
    assert sources == {"CURRENT_TASK", "HARD_CONSTRAINTS"}
    assert _items(context, ContextSectionName.HARD_CONSTRAINTS)


@pytest.mark.asyncio
async def test_deleted_expired_superseded_and_other_user_never_leak() -> None:
    container = memory_container()
    await seed_profile(container)
    service = container.memory_application_service
    deleted = await service.create_memory(
        container.development_user,
        CreateMemoryCommand(
            client_request_id="delete-me",
            memory_type=MemoryType.DISLIKED_ACTIVITY,
            key="disliked_activity",
            value="jumping",
            valid_until=None,
        ),
    )
    await service.delete_memory(container.development_user, deleted.record.memory.id, 1)
    old = await service.create_memory(
        container.development_user,
        CreateMemoryCommand(
            client_request_id="replace-me",
            memory_type=MemoryType.PREFERRED_TIME_OF_DAY,
            key="preferred_time_of_day",
            value="morning",
            valid_until=None,
        ),
    )
    await service.replace_memory(
        container.development_user,
        old.record.memory.id,
        ReplaceMemoryCommand(
            client_request_id="replace-it",
            expected_version=1,
            value="evening",
            valid_until=None,
        ),
    )
    expired = await service.create_memory(
        container.development_user,
        CreateMemoryCommand(
            client_request_id="expire-me",
            memory_type=MemoryType.PREFERRED_EQUIPMENT,
            key="preferred_equipment",
            value="band",
            valid_until=future(),
        ),
    )
    await container.memory_repository.list_active_for_context(
        container.development_user.id,
        expired.record.memory.valid_until + timedelta(seconds=1),
    )
    context = await container.context_application_service.build(
        container.development_user,
        ContextBuildCommand(
            agent_type=AgentType.PLAN_GENERATION,
            current_task={"request_type": "plan"},
        ),
    )
    values = _items(context, ContextSectionName.RELEVANT_CONFIRMED_MEMORIES)
    assert values == {"preferred_time_of_day": "evening"}
    with pytest.raises(MemoryNotFoundError):
        await container.context_application_service.get_audit(
            type(container.development_user)(
                id=uuid4(),
                email="other@fitweek.local",
                timezone="UTC",
                status=container.development_user.status,
                created_at=container.development_user.created_at,
                updated_at=container.development_user.updated_at,
                version=1,
            ),
            context.audit_id,
        )


@pytest.mark.asyncio
async def test_budget_keeps_mandatory_sections_and_trims_memory_stably() -> None:
    container = memory_container()
    await seed_profile(container)
    for index in range(5):
        await container.memory_application_service.create_memory(
            container.development_user,
            CreateMemoryCommand(
                client_request_id=f"budget-{index}",
                memory_type=MemoryType.DISLIKED_ACTIVITY,
                key=f"disliked_{index}",
                value=f"activity-{index}",
                valid_until=None,
            ),
        )
    with pytest.raises(ContextBudgetExceededError):
        await container.context_application_service.build(
            container.development_user,
            ContextBuildCommand(
                agent_type=AgentType.PLAN_GENERATION,
                current_task={"request_type": "plan"},
                max_characters=128,
            ),
        )
    context = await container.context_application_service.build(
        container.development_user,
        ContextBuildCommand(
            agent_type=AgentType.PLAN_GENERATION,
            current_task={"request_type": "plan"},
            max_characters=1800,
        ),
    )
    names = {section.name for section in context.sections}
    assert {
        ContextSectionName.SYSTEM_POLICY,
        ContextSectionName.CURRENT_TASK,
        ContextSectionName.PROFILE_SNAPSHOT,
        ContextSectionName.HARD_CONSTRAINTS,
        ContextSectionName.OUTPUT_CONTRACT,
    } <= names
    audit = await container.context_application_service.get_audit(
        container.development_user, context.audit_id
    )
    assert audit.budget_after <= 1800


@pytest.mark.asyncio
async def test_no_memory_retries_once_and_preserves_profile_constraints() -> None:
    container = memory_container()
    await seed_profile(container)
    await add_constraint(container, ConstraintType.EXCLUDED_FEATURE, "jumping")
    repository = container.memory_repository
    assert isinstance(repository, InMemoryMemoryRepository)
    repository.set_query_failures(2)
    context = await container.context_application_service.build(
        container.development_user,
        ContextBuildCommand(
            agent_type=AgentType.PLAN_GENERATION,
            current_task={"request_type": "plan"},
        ),
    )
    assert context.degraded_mode is ContextDegradedMode.NO_MEMORY
    assert _items(context, ContextSectionName.PROFILE_SNAPSHOT)
    assert _items(context, ContextSectionName.HARD_CONSTRAINTS)
    metrics = container.memory_application_service.metrics()
    assert metrics["memory_query_failures"] == 2
    assert metrics["context_no_memory_degraded"] == 1
