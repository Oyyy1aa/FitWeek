"""Immutable result types returned by the deterministic safety engine."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class SafetyViolation:
    """A single, client-safe safety rule violation."""

    code: str
    message: str
    path: str | None = None
    session_id: UUID | None = None
    exercise_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SafetyValidationResult:
    """The complete result of validating a submitted weekly plan."""

    passed: bool
    violations: tuple[SafetyViolation, ...] = ()

    def __post_init__(self) -> None:
        if self.passed == bool(self.violations):
            raise ValueError("passed must be true exactly when violations is empty")

    @classmethod
    def from_violations(
        cls,
        violations: tuple[SafetyViolation, ...],
    ) -> SafetyValidationResult:
        """Build a result while keeping the status consistent with its violations."""

        return cls(passed=not violations, violations=violations)
