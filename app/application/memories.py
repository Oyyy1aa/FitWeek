"""Current-user application boundary for Memory and Candidate use cases."""

from datetime import timedelta
from uuid import UUID, uuid4

from app.domain.memory.errors import (
    MemoryCandidateIdempotencyConflictError,
    MemoryConflictError,
    MemoryInvalidValueError,
)
from app.domain.memory.models import (
    CandidateReviewOutcome,
    CandidateWriteOutcome,
    MemoryCandidate,
    MemoryRecord,
    MemoryReplaceOutcome,
    MemoryWriteOutcome,
)
from app.domain.tools.enums import ToolCaller, ToolId, ToolInvocationStatus
from app.domain.tools.models import ToolInvocationContext
from app.domain.users.models import UserAccount
from app.memory.candidate_service import (
    AcceptCandidateCommand,
    CreateCandidateCommand,
    MemoryCandidateService,
    RejectCandidateCommand,
)
from app.memory.service import (
    CreateMemoryCommand,
    MemoryService,
    ReplaceMemoryCommand,
    UpdateMemoryCommand,
)
from app.observability.facade import ObservabilityFacade
from app.tool_adapters.contracts import (
    MemoryCandidateCreateRequest,
    MemoryCandidateCreateResponse,
)
from app.tool_gateway.gateway import ToolGateway


class MemoryApplicationService:
    def __init__(
        self,
        memories: MemoryService,
        candidates: MemoryCandidateService,
        tool_gateway: ToolGateway | None = None,
        observability: ObservabilityFacade | None = None,
    ) -> None:
        self._memories = memories
        self._candidates = candidates
        self._tool_gateway = tool_gateway
        self._observability = observability

    def attach_tool_gateway(self, gateway: ToolGateway) -> None:
        self._tool_gateway = gateway

    async def create_memory(
        self, user: UserAccount, command: CreateMemoryCommand
    ) -> MemoryWriteOutcome:
        return await self._memories.create_explicit_memory(user.id, command)

    async def get_memory(self, user: UserAccount, memory_id: UUID) -> MemoryRecord:
        return await self._memories.get_memory(user.id, memory_id)

    async def list_memories(self, user: UserAccount) -> list[MemoryRecord]:
        return await self._memories.list_memories(user.id)

    async def update_memory(
        self,
        user: UserAccount,
        memory_id: UUID,
        command: UpdateMemoryCommand,
    ) -> MemoryRecord:
        return await self._memories.update_memory(user.id, memory_id, command)

    async def delete_memory(
        self, user: UserAccount, memory_id: UUID, expected_version: int
    ) -> MemoryRecord:
        return await self._memories.delete_memory(user.id, memory_id, expected_version)

    async def replace_memory(
        self,
        user: UserAccount,
        memory_id: UUID,
        command: ReplaceMemoryCommand,
    ) -> MemoryReplaceOutcome:
        return await self._memories.replace_memory(user.id, memory_id, command)

    async def create_candidate(
        self, user: UserAccount, command: CreateCandidateCommand
    ) -> CandidateWriteOutcome:
        if self._tool_gateway is None:
            return await self._candidates.create_candidate(user.id, command)
        now = self._tool_gateway.clock.now()
        outcome = await self._tool_gateway.invoke(
            ToolInvocationContext(
                invocation_id=uuid4(),
                correlation_id=uuid4(),
                user_id=user.id,
                caller=ToolCaller.MEMORY_COMMITTER,
                tool_id=ToolId.MEMORY_CANDIDATE_CREATE,
                tool_version="phase-8a-v1",
                deadline_at=now + timedelta(seconds=2),
                created_at=now,
                idempotency_key=command.client_request_id,
            ),
            MemoryCandidateCreateRequest(user_id=user.id, command=command),
        )
        if outcome.result.status is not ToolInvocationStatus.SUCCEEDED:
            errors = {
                "MEMORY_CANDIDATE_IDEMPOTENCY_CONFLICT": (
                    MemoryCandidateIdempotencyConflictError
                ),
                "MEMORY_CONFLICT": MemoryConflictError,
                "MEMORY_INVALID_VALUE": MemoryInvalidValueError,
            }
            error_type = errors.get(outcome.result.error_code or "")
            if error_type is not None:
                raise error_type("Memory Candidate creation was rejected.")
            raise RuntimeError("Memory Candidate creation is unavailable.")
        if not isinstance(outcome.response, MemoryCandidateCreateResponse):
            raise RuntimeError("Memory Candidate creation is unavailable.")
        candidate = await self._candidates.get_candidate(
            user.id, outcome.response.candidate_id
        )
        if self._observability is not None:
            self._observability.record_counter(
                "fitweek_memory_candidates_created_total",
                labels={
                    "outcome": (
                        "REUSED" if outcome.response.idempotent_reuse else "CREATED"
                    )
                },
            )
        return CandidateWriteOutcome(
            candidate=candidate,
            created=not outcome.response.idempotent_reuse,
        )

    async def get_candidate(
        self, user: UserAccount, candidate_id: UUID
    ) -> MemoryCandidate:
        return await self._candidates.get_candidate(user.id, candidate_id)

    async def list_candidates(self, user: UserAccount) -> list[MemoryCandidate]:
        return await self._candidates.list_candidates(user.id)

    async def accept_candidate(
        self,
        user: UserAccount,
        candidate_id: UUID,
        command: AcceptCandidateCommand,
    ) -> CandidateReviewOutcome:
        outcome = await self._candidates.accept_candidate(
            user.id, candidate_id, command
        )
        if self._observability is not None:
            self._observability.record_counter(
                "fitweek_memory_candidates_accepted_total",
                labels={"outcome": "ACCEPTED"},
            )
        return outcome

    async def reject_candidate(
        self,
        user: UserAccount,
        candidate_id: UUID,
        command: RejectCandidateCommand,
    ) -> CandidateReviewOutcome:
        return await self._candidates.reject_candidate(user.id, candidate_id, command)

    def metrics(self) -> dict[str, int]:
        return self._memories.metrics()
