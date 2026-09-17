"""Cache-failure isolation for the ACTIVE Memory read-through boundary."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.domain.memory.enums import MemoryType
from app.memory.metrics import MemoryMetrics
from app.memory.retrieval import MemoryRetriever
from app.memory.service import CreateMemoryCommand, MemoryService
from app.persistence.memory.memory_repository import InMemoryMemoryRepository
from app.persistence.memory.store import InMemoryStore


class FailingCache:
    def __init__(
        self,
        *,
        fail_get: bool = False,
        fail_set: bool = False,
        fail_delete: bool = False,
    ) -> None:
        self.fail_get = fail_get
        self.fail_set = fail_set
        self.fail_delete = fail_delete

    async def get_active(self, _user_id):
        if self.fail_get:
            raise TimeoutError("bounded cache timeout")
        return None

    async def set_active(self, _user_id, _memories) -> None:
        if self.fail_set:
            raise TimeoutError("bounded cache timeout")

    async def invalidate_active(self, _user_id) -> None:
        if self.fail_delete:
            raise TimeoutError("bounded cache timeout")


async def _memory_service(
    cache: FailingCache,
) -> tuple[MemoryService, MemoryRetriever, object]:
    repository = InMemoryMemoryRepository(InMemoryStore())
    metrics = MemoryMetrics()
    service = MemoryService(repository, metrics, cache)
    user_id = uuid4()
    await service.create_explicit_memory(
        user_id,
        CreateMemoryCommand(
            client_request_id="cache-unit-memory",
            memory_type=MemoryType.PREFERRED_LOCATION,
            key="preferred_location",
            value="HOME",
            valid_until=None,
        ),
    )
    return service, MemoryRetriever(repository, metrics, cache), user_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cache", [FailingCache(fail_get=True), FailingCache(fail_set=True)]
)
async def test_cache_get_or_set_timeout_falls_back_to_memory_repository(
    cache: FailingCache,
) -> None:
    _service, retriever, user_id = await _memory_service(cache)

    outcome = await retriever.retrieve(user_id, datetime.now(UTC))

    assert [item.display_value for item in outcome.result.memories] == ["HOME"]
    assert outcome.degraded_mode.value == "NONE"


@pytest.mark.asyncio
async def test_invalidation_timeout_does_not_rollback_memory_write() -> None:
    cache = FailingCache(fail_delete=True)
    service, retriever, user_id = await _memory_service(cache)

    outcome = await service.create_explicit_memory(
        user_id,
        CreateMemoryCommand(
            client_request_id="cache-unit-memory-second",
            memory_type=MemoryType.PREFERRED_TIME_OF_DAY,
            key="preferred_time_of_day",
            value="MORNING",
            valid_until=None,
        ),
    )
    retrieved = await retriever.retrieve(user_id, datetime.now(UTC))

    assert outcome.created is True
    assert {item.key for item in retrieved.result.memories} == {
        "preferred_location",
        "preferred_time_of_day",
    }
