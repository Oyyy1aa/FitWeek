"""Compatibility exports for Context-specific domain failures."""

from app.domain.memory.errors import (
    ContextBudgetExceededError,
    ContextContractNotFoundError,
    ContextContractVersionNotFoundError,
    ContextDebugApiDisabledError,
    ContextInjectionFailedError,
    ContextSnapshotNotFoundError,
    ContextSnapshotVersionMismatchError,
    ContextUserIsolationError,
)

__all__ = [
    "ContextBudgetExceededError",
    "ContextContractNotFoundError",
    "ContextContractVersionNotFoundError",
    "ContextDebugApiDisabledError",
    "ContextInjectionFailedError",
    "ContextSnapshotNotFoundError",
    "ContextSnapshotVersionMismatchError",
    "ContextUserIsolationError",
]
