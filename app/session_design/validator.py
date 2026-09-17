"""Strict whitelist and business validation for untrusted model output."""

from collections.abc import Mapping, Sequence

from app.domain.exercises.models import Exercise, ExerciseStatus
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.domain.session_design.errors import SessionDesignValidationError
from app.domain.session_design.models import ExerciseCandidateSet, SessionDesignerOutput
from app.domain.session_design.templates import SessionTemplate
from app.safety.constraint_validator import ConstraintValidator


class SessionDesignerBusinessValidator:
    def __init__(self, constraints: ConstraintValidator | None = None) -> None:
        self._constraints = constraints or ConstraintValidator()

    def validate(
        self,
        output: SessionDesignerOutput,
        *,
        candidate_set: ExerciseCandidateSet,
        template: SessionTemplate,
        profile: FitnessProfile,
        constraints: Sequence[UserConstraint],
        catalog: Mapping[str, Exercise],
    ) -> SessionDesignerOutput:
        if (
            output.template_id is not template.id
            or output.session_type is not template.session_type
        ):
            raise SessionDesignValidationError(
                "TEMPLATE_MISMATCH", "The output changed the frozen template contract."
            )
        if len(output.selections) != len(candidate_set.slots):
            raise SessionDesignValidationError(
                "SLOT_COVERAGE_MISMATCH",
                "Every frozen template slot must be selected once.",
            )
        selected_slots = tuple(item.slot_id for item in output.selections)
        expected_slots = tuple(item.slot_id for item in candidate_set.slots)
        if selected_slots != expected_slots:
            raise SessionDesignValidationError(
                "SLOT_ORDER_MISMATCH", "Selections must follow the frozen slot order."
            )
        exercise_ids = tuple(item.exercise_id for item in output.selections)
        if len(exercise_ids) != len(set(exercise_ids)):
            raise SessionDesignValidationError(
                "DUPLICATE_EXERCISE", "A Session Draft cannot repeat an exercise."
            )
        for selection in output.selections:
            if not candidate_set.allowed(selection.slot_id, selection.exercise_id):
                raise SessionDesignValidationError(
                    "EXERCISE_OUTSIDE_CANDIDATE_SET",
                    "The model selected an exercise outside the frozen Candidate Set.",
                )
            exercise = catalog.get(selection.exercise_id)
            if exercise is None or exercise.status is not ExerciseStatus.ACTIVE:
                raise SessionDesignValidationError(
                    "EXERCISE_NOT_ACTIVE", "The selected exercise is not active."
                )
        explanation = output.explanation_summary.casefold()
        if "```" in output.explanation_summary or "ignore system" in explanation:
            raise SessionDesignValidationError(
                "SESSION_DESIGN_BUSINESS_INVALID",
                "Explanation contains instruction-like content.",
            )
        return output
