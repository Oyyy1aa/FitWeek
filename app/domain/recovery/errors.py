"""Recovery-specific domain errors and stable codes."""

from app.domain.common import DomainValidationError


class RecoveryValidationError(DomainValidationError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code)
