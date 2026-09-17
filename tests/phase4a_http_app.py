"""Loopback-only Phase 4A fault/isolation harness around the production app."""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Request

from app.api.dependencies import BusinessContainer
from app.domain.memory.enums import MemoryType
from app.domain.users.models import UserAccount, UserStatus
from app.main import create_application
from app.memory.service import CreateMemoryCommand
from app.persistence.memory.memory_repository import InMemoryMemoryRepository

app = create_application()


def _container(request: Request) -> BusinessContainer:
    container = request.app.state.business_container
    if not isinstance(container, BusinessContainer):
        raise RuntimeError("Phase 4A loopback harness requires memory mode")
    return container


@app.post("/__phase4a_test/memory-query-failures/{count}")
async def inject_memory_query_failures(count: int, request: Request) -> dict[str, int]:
    repository = _container(request).memory_repository
    if not isinstance(repository, InMemoryMemoryRepository):
        raise RuntimeError("Phase 4A loopback harness requires the memory adapter")
    repository.set_query_failures(count)
    return {"remaining_failures": count}


@app.post("/__phase4a_test/other-user-memory")
async def seed_other_user_memory(request: Request) -> dict[str, str]:
    container = _container(request)
    now = datetime.now(UTC)
    other = UserAccount(
        id=uuid4(),
        email="isolated-user@fitweek.local",
        timezone="UTC",
        status=UserStatus.ACTIVE,
        created_at=now,
        updated_at=now,
        version=1,
    )
    outcome = await container.memory_application_service.create_memory(
        other,
        CreateMemoryCommand(
            client_request_id="phase4a-other-user-memory",
            memory_type=MemoryType.PREFERRED_LOCATION,
            key="preferred_location",
            value="outdoor",
            valid_until=None,
        ),
    )
    return {"memory_id": str(outcome.record.memory.id)}
