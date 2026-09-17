"""Persistence- and HTTP-independent Memory and Context failures."""


class MemoryDomainError(RuntimeError):
    code = "MEMORY_ERROR"


class MemoryNotFoundError(MemoryDomainError):
    code = "MEMORY_NOT_FOUND"


class MemoryVersionConflictError(MemoryDomainError):
    code = "MEMORY_VERSION_CONFLICT"


class MemoryIdempotencyConflictError(MemoryDomainError):
    code = "MEMORY_IDEMPOTENCY_CONFLICT"


class MemoryInvalidTypeError(MemoryDomainError):
    code = "MEMORY_INVALID_TYPE"


class MemoryInvalidValueError(MemoryDomainError):
    code = "MEMORY_INVALID_VALUE"


class MemoryConflictError(MemoryDomainError):
    code = "MEMORY_CONFLICT"


class MemoryNotActiveError(MemoryDomainError):
    code = "MEMORY_NOT_ACTIVE"


class MemoryAlreadyDeletedError(MemoryDomainError):
    code = "MEMORY_ALREADY_DELETED"


class MemoryEvidenceRequiredError(MemoryDomainError):
    code = "MEMORY_EVIDENCE_REQUIRED"


class MemoryCandidateNotFoundError(MemoryDomainError):
    code = "MEMORY_CANDIDATE_NOT_FOUND"


class MemoryCandidateExpiredError(MemoryDomainError):
    code = "MEMORY_CANDIDATE_EXPIRED"


class MemoryCandidateRejectedError(MemoryDomainError):
    code = "MEMORY_CANDIDATE_REJECTED"


class MemoryCandidateAlreadyAcceptedError(MemoryDomainError):
    code = "MEMORY_CANDIDATE_ALREADY_ACCEPTED"


class MemoryCandidateVersionConflictError(MemoryDomainError):
    code = "MEMORY_CANDIDATE_VERSION_CONFLICT"


class MemoryCandidateIdempotencyConflictError(MemoryDomainError):
    code = "MEMORY_CANDIDATE_IDEMPOTENCY_CONFLICT"


class MemoryQueryFailedError(MemoryDomainError):
    code = "MEMORY_QUERY_FAILED"


class ContextContractNotFoundError(MemoryDomainError):
    code = "CONTEXT_CONTRACT_NOT_FOUND"


class ContextBudgetExceededError(MemoryDomainError):
    code = "CONTEXT_BUDGET_EXCEEDED"


class ContextUserIsolationError(MemoryDomainError):
    code = "CONTEXT_USER_ISOLATION_VIOLATION"


class ContextDebugApiDisabledError(MemoryDomainError):
    code = "CONTEXT_DEBUG_API_DISABLED"


class ContextSnapshotNotFoundError(MemoryDomainError):
    code = "CONTEXT_SNAPSHOT_NOT_FOUND"


class ContextSnapshotVersionMismatchError(MemoryDomainError):
    code = "CONTEXT_SNAPSHOT_VERSION_MISMATCH"


class ContextContractVersionNotFoundError(ContextContractNotFoundError):
    code = "CONTEXT_CONTRACT_VERSION_NOT_FOUND"


class ContextInjectionFailedError(MemoryDomainError):
    code = "CONTEXT_INJECTION_FAILED"


class DraftMemoryCandidateIndexInvalidError(MemoryDomainError):
    code = "DRAFT_MEMORY_CANDIDATE_INDEX_INVALID"


class DraftMemoryCandidateUnsupportedError(MemoryDomainError):
    code = "DRAFT_MEMORY_CANDIDATE_UNSUPPORTED"


class DraftMemoryCandidateConflictError(MemoryDomainError):
    code = "DRAFT_MEMORY_CANDIDATE_CONFLICT"


class DraftMemoryCandidateImportIdempotencyConflictError(MemoryDomainError):
    code = "DRAFT_MEMORY_CANDIDATE_IMPORT_IDEMPOTENCY_CONFLICT"


class DraftMemoryCandidateAlreadyImportedError(MemoryDomainError):
    code = "DRAFT_MEMORY_CANDIDATE_ALREADY_IMPORTED"


class PlanGenerationContextConflictError(MemoryDomainError):
    code = "PLAN_GENERATION_CONTEXT_CONFLICT"
