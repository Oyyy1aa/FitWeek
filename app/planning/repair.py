"""Bounded deterministic regeneration for repairable safety violations."""

from app.domain.exercises.models import Exercise
from app.domain.planning.models import GenerateWeeklyPlanCommand, GenerationMetadata
from app.domain.plans.models import WeeklyPlan
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.planning.context_preferences import PlanningContextPreferences
from app.planning.generator import DeterministicPlanGenerator
from app.safety.models import SafetyViolation

_REPAIRABLE_CODES = frozenset(
    {
        "SESSION_TOO_LONG",
        "SESSION_DURATION_MISMATCH",
        "DUPLICATE_SEQUENCE",
        "EQUIPMENT_MISMATCH",
        "EXCLUDED_FEATURE",
        "SESSION_OVERLAP",
    }
)


class PlanRepairer:
    """Rebuild a candidate with a deterministic variant; never mutates a plan."""

    def __init__(self, generator: DeterministicPlanGenerator) -> None:
        self._generator = generator

    def repair(
        self,
        *,
        plan: WeeklyPlan,
        violations: tuple[SafetyViolation, ...],
        profile: FitnessProfile,
        constraints: tuple[UserConstraint, ...],
        catalog: tuple[Exercise, ...],
        command: GenerateWeeklyPlanCommand,
        attempt: int,
        context_preferences: PlanningContextPreferences | None = None,
        metadata: GenerationMetadata | None = None,
    ) -> WeeklyPlan | None:
        del plan
        if not violations or any(
            violation.code not in _REPAIRABLE_CODES for violation in violations
        ):
            return None
        return self._generator.generate_candidate(
            user_id=profile.user_id,
            profile=profile,
            constraints=constraints,
            catalog=catalog,
            command=command,
            context_preferences=context_preferences,
            context_snapshot_reference_id=(
                metadata.context_snapshot_reference_id if metadata else None
            ),
            context_fingerprint=metadata.context_fingerprint if metadata else "none",
            context_contract_version=(
                metadata.context_contract_version if metadata else "none"
            ),
            context_policy_version=(
                metadata.context_policy_version if metadata else "none"
            ),
            context_degradation_state=(
                metadata.context_degradation_state if metadata else "NONE"
            ),
            included_memory_count=(metadata.included_memory_count if metadata else 0),
            shadowed_memory_count=(metadata.shadowed_memory_count if metadata else 0),
            variant=attempt,
        ).plan
