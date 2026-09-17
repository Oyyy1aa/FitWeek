"""Stable behavior-policy errors."""

from app.domain.common import DomainValidationError


class BehaviorWindowError(DomainValidationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="BEHAVIOR_WINDOW_INVALID")


class BehaviorCheckInConflictError(DomainValidationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="BEHAVIOR_CHECKIN_CONFLICT")
