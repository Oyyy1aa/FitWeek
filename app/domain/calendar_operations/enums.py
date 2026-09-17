"""Controlled Calendar write lifecycle enumerations."""

from enum import StrEnum


class CalendarOperationType(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    KEEP = "KEEP"


class CalendarOperationDraftStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    PARTIALLY_SUCCEEDED = "PARTIALLY_SUCCEEDED"
    SUCCEEDED = "SUCCEEDED"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    CANCELLED = "CANCELLED"


class CalendarOperationItemStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    SKIPPED = "SKIPPED"


class CalendarBindingStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"
    ORPHANED = "ORPHANED"


class CalendarAttemptOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
