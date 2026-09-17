"""Application composition decorator for the pure Safety Engine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import uuid4

from app.domain.exercises.models import Exercise
from app.domain.plans.models import WeeklyPlan
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.observability.context import (
    ObservabilityContext,
    current_observability_context,
)
from app.observability.facade import ObservabilityFacade
from app.safety.engine import SafetyEngine
from app.safety.models import SafetyValidationResult


class ObservedSafetyEngine(SafetyEngine):
    """Delegate validation unchanged while adding a safe application span."""

    def __init__(
        self,
        delegate: SafetyEngine,
        observability: ObservabilityFacade,
    ) -> None:
        self._delegate = delegate
        self._observability = observability
        # Preserve every existing SafetyEngine entry point. Only validate_plan is
        # decorated below; inherited session validation continues unchanged.
        super().__init__(
            scope_guard=delegate._scope_guard,
            constraint_validator=delegate._constraints,
        )

    def validate_plan(
        self,
        *,
        profile: FitnessProfile,
        constraints: Sequence[UserConstraint],
        plan: WeeklyPlan,
        exercise_catalog: Mapping[str, Exercise],
    ) -> SafetyValidationResult:
        inherited = current_observability_context()
        context = ObservabilityContext(
            correlation_id=(
                inherited.correlation_id if inherited is not None else uuid4()
            ),
            request_id=inherited.request_id if inherited is not None else None,
            run_id=inherited.run_id if inherited is not None else None,
            step_id=inherited.step_id if inherited is not None else None,
            operation_name="safety.validate_plan",
            component="safety",
        )
        with self._observability.operation(
            "safety.validation", context=context
        ) as span:
            result = self._delegate.validate_plan(
                profile=profile,
                constraints=constraints,
                plan=plan,
                exercise_catalog=exercise_catalog,
            )
            if result.passed:
                span.succeed()
            else:
                span.reject("BUSINESS_REJECTED", "SAFETY_VALIDATION_FAILED")
            return result
