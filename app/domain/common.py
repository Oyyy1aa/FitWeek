"""Shared domain primitives and validation helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum


class DomainValidationError(ValueError):
    """Raised when a domain value violates an intrinsic invariant."""

    def __init__(self, message: str, *, code: str = "DOMAIN_VALIDATION_ERROR") -> None:
        super().__init__(message)
        self.code = code


class DomainConflictError(RuntimeError):
    """Raised when a version precondition does not match current state."""


class InvalidDomainStateTransition(RuntimeError):
    """Raised when an aggregate cannot perform the requested transition."""


class RepositoryError(RuntimeError):
    """Base error shared by repository contracts and their adapters."""


class RepositoryConflictError(RepositoryError):
    """Raised when an update fails an optimistic-lock precondition."""

    def __init__(
        self,
        resource: str,
        resource_id: object,
        *,
        expected_version: int,
        actual_version: int,
    ) -> None:
        self.resource = resource
        self.resource_id = resource_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"{resource} {resource_id!s} version conflict: "
            f"expected {expected_version}, received {actual_version}"
        )


class RepositoryUniqueError(RepositoryError):
    """Raised when a repository uniqueness rule would be violated."""

    def __init__(self, constraint: str, value: object) -> None:
        self.constraint = constraint
        self.value = value
        super().__init__(f"unique constraint {constraint!r} rejected {value!s}")


class LocationType(StrEnum):
    """Supported MVP workout locations."""

    HOME = "HOME"
    GYM = "GYM"
    OUTDOOR = "OUTDOOR"


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def require_datetime(value: datetime, field_name: str) -> None:
    """Require a datetime without imposing candidate-plan safety semantics."""

    if not isinstance(value, datetime):
        raise DomainValidationError(f"{field_name} must be a datetime.")


def require_utc_datetime(value: datetime, field_name: str) -> None:
    """Require a timezone-aware datetime whose offset is UTC."""

    require_datetime(value, field_name)
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(
            f"{field_name} must be timezone-aware.", code="NAIVE_DATETIME"
        )
    if value.utcoffset() != timedelta(0):
        raise DomainValidationError(f"{field_name} must use UTC.")


def require_version(version: int) -> None:
    """Require a positive optimistic-lock version."""

    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise DomainValidationError("version must be a positive integer.")


def require_non_blank(value: str, field_name: str) -> None:
    """Require a non-empty string after trimming whitespace."""

    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must not be blank.")
