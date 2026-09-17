"""Product-scope guard for plan validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.safety.models import SafetyViolation

if TYPE_CHECKING:
    from app.domain.profiles.models import FitnessProfile


class ScopeGuard:
    """Enforce the explicitly confirmed Phase 1A product boundary."""

    def validate(self, profile: FitnessProfile) -> tuple[SafetyViolation, ...]:
        """Reject plan creation until the user confirms the supported scope."""

        if profile.scope_confirmed:
            return ()
        return (
            SafetyViolation(
                code="SCOPE_NOT_CONFIRMED",
                message="The supported fitness-planning scope has not been confirmed.",
                path="profile.scope_confirmed",
            ),
        )
