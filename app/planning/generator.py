"""Pure deterministic candidate-plan orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from app.domain.exercises.models import Exercise
from app.domain.planning.models import (
    GenerateWeeklyPlanCommand,
    GenerationFailureReason,
    GenerationMetadata,
    PlanGenerationError,
)
from app.domain.planning.policies import (
    DEFAULT_GENERATION_POLICY,
    GenerationPolicy,
)
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.profiles.models import FitnessGoal, FitnessProfile, UserConstraint
from app.domain.sessions.models import SessionType, WorkoutSession
from app.planning.context_preferences import PlanningContextPreferences
from app.planning.exercise_selector import ExerciseSelector
from app.planning.session_builder import SessionBuilder
from app.planning.slot_selector import SlotSelector

_PLAN_ID_NAMESPACE = UUID("c03fd88d-f0a4-47d7-8c6e-7128fc89b859")


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerationCandidate:
    plan: WeeklyPlan
    metadata: GenerationMetadata


class DeterministicPlanGenerator:
    """Construct a reproducible candidate without repositories, HTTP, or randomness."""

    def __init__(
        self,
        *,
        policy: GenerationPolicy = DEFAULT_GENERATION_POLICY,
        slot_selector: SlotSelector | None = None,
        exercise_selector: ExerciseSelector | None = None,
        session_builder: SessionBuilder | None = None,
    ) -> None:
        self.policy = policy
        self._slots = slot_selector or SlotSelector()
        self._exercises = exercise_selector or ExerciseSelector()
        self._sessions = session_builder or SessionBuilder()

    def generate_candidate(
        self,
        *,
        user_id: UUID,
        profile: FitnessProfile,
        constraints: tuple[UserConstraint, ...],
        catalog: tuple[Exercise, ...],
        command: GenerateWeeklyPlanCommand,
        context_preferences: PlanningContextPreferences | None = None,
        context_snapshot_reference_id: UUID | None = None,
        context_fingerprint: str = "none",
        context_contract_version: str = "none",
        context_policy_version: str = "none",
        context_degradation_state: str = "NONE",
        included_memory_count: int = 0,
        shadowed_memory_count: int = 0,
        variant: int = 0,
    ) -> GenerationCandidate:
        if not profile.scope_confirmed:
            raise PlanGenerationError(
                GenerationFailureReason(
                    code="SCOPE_NOT_CONFIRMED",
                    message="Confirm the supported fitness scope before generation.",
                )
            )
        catalog_version = self._catalog_version(catalog)
        fingerprint = self._input_fingerprint(
            user_id=user_id,
            profile=profile,
            constraints=constraints,
            catalog_version=catalog_version,
            command=command,
            context_fingerprint=context_fingerprint,
            context_contract_version=context_contract_version,
            context_policy_version=context_policy_version,
            context_degradation_state=context_degradation_state,
        )
        plan_id = uuid5(_PLAN_ID_NAMESPACE, fingerprint)
        selected_slots = self._slots.select(
            profile=profile,
            constraints=constraints,
            command=command,
            policy=self.policy,
            context_preferences=context_preferences,
            variant=variant,
        )
        sessions = []
        for index, slot in enumerate(selected_slots):
            session_type = self._session_type(
                profile.primary_goal,
                command.preferred_session_types,
                index,
            )
            exercises = self._exercises.select(
                profile=profile,
                constraints=constraints,
                catalog=catalog,
                location=slot.location_type,
                session_type=session_type,
                at=slot.start,
                session_index=index,
                policy=self.policy,
                context_preferences=context_preferences,
                variant=variant,
            )
            sessions.append(
                self._sessions.build(
                    plan_id=plan_id,
                    session_index=index,
                    slot=slot,
                    session_type=session_type,
                    exercises=exercises,
                    profile=profile,
                )
            )

        preference_fallbacks = self._preference_fallbacks(sessions, context_preferences)
        metadata = GenerationMetadata(
            generation_policy_version=self.policy.version,
            catalog_version=catalog_version,
            input_fingerprint=fingerprint,
            context_snapshot_reference_id=context_snapshot_reference_id,
            context_fingerprint=context_fingerprint,
            context_contract_version=context_contract_version,
            context_policy_version=context_policy_version,
            context_degradation_state=context_degradation_state,
            included_memory_count=included_memory_count,
            shadowed_memory_count=shadowed_memory_count,
            preference_fallbacks=preference_fallbacks,
        )

        now = datetime.now(UTC)
        plan = WeeklyPlan(
            id=plan_id,
            user_id=user_id,
            week_start=command.week_start,
            status=WeeklyPlanStatus.DRAFT,
            revision=1,
            goal_snapshot={
                "experience_level": profile.experience_level.value,
                "primary_goal": profile.primary_goal.value,
                "weekly_frequency": profile.weekly_frequency,
                "max_session_minutes": profile.max_session_minutes,
            },
            constraint_snapshot=tuple(
                self._constraint_snapshot(item) for item in constraints
            ),
            estimated_total_minutes=sum(
                session.estimated_minutes for session in sessions
            ),
            sessions=tuple(sessions),
            created_at=now,
            updated_at=now,
            confirmed_at=None,
            version=1,
            generation_metadata=metadata.as_dict(),
        )
        return GenerationCandidate(plan=plan, metadata=metadata)

    @staticmethod
    def _session_type(
        goal: FitnessGoal,
        preferred: tuple[SessionType, ...],
        index: int,
    ) -> SessionType:
        if preferred:
            return preferred[index % len(preferred)]
        if goal is FitnessGoal.BASIC_STRENGTH:
            return SessionType.STRENGTH
        if goal is FitnessGoal.LOW_IMPACT_CARDIO:
            return SessionType.CARDIO
        if goal is FitnessGoal.MOBILITY:
            return SessionType.MOBILITY
        return SessionType.MIXED

    def _input_fingerprint(
        self,
        *,
        user_id: UUID,
        profile: FitnessProfile,
        constraints: tuple[UserConstraint, ...],
        catalog_version: str,
        command: GenerateWeeklyPlanCommand,
        context_fingerprint: str,
        context_contract_version: str,
        context_policy_version: str,
        context_degradation_state: str,
    ) -> str:
        payload = {
            "policy": self.policy.version,
            "context_fingerprint": context_fingerprint,
            "context_contract_version": context_contract_version,
            "context_policy_version": context_policy_version,
            "context_degradation_state": context_degradation_state,
            "catalog": catalog_version,
            "user_id": str(user_id),
            "profile": {
                "experience_level": profile.experience_level.value,
                "weekly_frequency": profile.weekly_frequency,
                "max_session_minutes": profile.max_session_minutes,
                "primary_goal": profile.primary_goal.value,
                "scope_confirmed": profile.scope_confirmed,
                "version": profile.version,
            },
            "constraints": [
                {
                    "type": item.constraint_type.value,
                    "value": item.constraint_value,
                    "priority": item.priority,
                    "hard": item.is_hard,
                    "valid_until": (
                        item.valid_until.astimezone(UTC).isoformat()
                        if item.valid_until is not None
                        else None
                    ),
                    "version": item.version,
                }
                for item in sorted(
                    constraints,
                    key=lambda item: (
                        item.constraint_type.value,
                        item.constraint_value,
                        item.priority,
                        str(item.id),
                    ),
                )
            ],
            "week_start": command.week_start.isoformat(),
            "availability": [
                {
                    "start": slot.start_utc.isoformat(),
                    "end": slot.end_utc.isoformat(),
                    "location": slot.location_type.value,
                }
                for slot in sorted(
                    command.availability_slots,
                    key=lambda slot: (
                        slot.start_utc,
                        slot.end_utc,
                        slot.location_type.value,
                    ),
                )
            ],
            "preferred_locations": [item.value for item in command.preferred_locations],
            "preferred_session_types": [
                item.value for item in command.preferred_session_types
            ],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _preference_fallbacks(
        sessions: list[WorkoutSession],
        preferences: PlanningContextPreferences | None,
    ) -> tuple[str, ...]:
        if preferences is None or not preferences.disliked_activities:
            return ()
        disliked = {item.casefold() for item in preferences.disliked_activities}
        for session in sessions:
            if any(
                item.exercise_id.casefold() in disliked for item in session.exercises
            ):
                return ("DISLIKED_ACTIVITY_REQUIRED_FOR_SAFE_VARIETY",)
        return ()

    @staticmethod
    def _catalog_version(catalog: tuple[Exercise, ...]) -> str:
        canonical = "|".join(
            f"{item.id}:{item.version}"
            for item in sorted(catalog, key=lambda item: item.id)
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _constraint_snapshot(constraint: UserConstraint) -> dict[str, object]:
        return {
            "id": str(constraint.id),
            "constraint_type": constraint.constraint_type.value,
            "constraint_value": constraint.constraint_value,
            "priority": constraint.priority,
            "is_hard": constraint.is_hard,
            "source": constraint.source.value,
            "valid_until": (
                constraint.valid_until.isoformat()
                if constraint.valid_until is not None
                else None
            ),
            "version": constraint.version,
        }
