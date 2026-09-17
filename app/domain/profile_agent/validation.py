"""Deterministic business validation after Profile Agent schema parsing."""

from app.domain.model_gateway.errors import ModelBusinessValidationError
from app.domain.profile_agent.models import ProfileAgentInput, ProfileAgentOutput
from app.domain.profiles.models import ConstraintType

_MEDICAL_OUTPUT_TERMS = (
    "diagnosis",
    "prescription",
    "rehabilitation program",
    "诊断",
    "处方",
    "康复方案",
)


class ProfileAgentBusinessValidator:
    """Reject unknown catalog values and medical conclusions without coercion."""

    def validate(
        self,
        output: ProfileAgentOutput,
        input_value: ProfileAgentInput,
    ) -> ProfileAgentOutput:
        if any(goal.value not in input_value.supported_goals for goal in output.goals):
            raise ModelBusinessValidationError(
                "The response contained an unsupported goal."
            )
        if any(
            item not in input_value.supported_equipment for item in output.equipment
        ):
            raise ModelBusinessValidationError(
                "The response contained unsupported equipment."
            )
        if any(
            location.value not in input_value.supported_locations
            for location in output.locations
        ):
            raise ModelBusinessValidationError(
                "The response contained an unsupported location."
            )
        proposals = (*output.hard_constraints, *output.temporary_constraints)
        if any(
            item.constraint_type.value not in input_value.supported_constraint_types
            for item in proposals
        ):
            raise ModelBusinessValidationError(
                "The response contained an unsupported constraint type."
            )
        if any(not item.is_hard for item in output.hard_constraints):
            raise ModelBusinessValidationError("Hard constraints must be marked hard.")
        for item in proposals:
            self._validate_constraint_value(
                item.constraint_type, item.value, input_value
            )
        text_values = [
            output.explanation_summary,
            *(item.value for item in proposals),
            *(item.value for item in output.soft_preferences),
            *(item.value for item in output.memory_candidates),
        ]
        if any(
            term in value.casefold()
            for value in text_values
            for term in _MEDICAL_OUTPUT_TERMS
        ):
            raise ModelBusinessValidationError(
                "The response contained an unsupported medical conclusion."
            )
        return output

    @staticmethod
    def _validate_constraint_value(
        constraint_type: ConstraintType,
        raw_value: str,
        input_value: ProfileAgentInput,
    ) -> None:
        value = raw_value.strip()
        if constraint_type is ConstraintType.AVAILABLE_EQUIPMENT:
            if value not in {*input_value.supported_equipment, "none"}:
                raise ModelBusinessValidationError(
                    "The response proposed unsupported available equipment."
                )
        elif constraint_type is ConstraintType.ALLOWED_LOCATION:
            if value.upper() not in input_value.supported_locations:
                raise ModelBusinessValidationError(
                    "The response proposed an unsupported location constraint."
                )
        elif constraint_type is ConstraintType.MAX_SESSION_MINUTES:
            try:
                minutes = int(value)
            except ValueError as exc:
                raise ModelBusinessValidationError(
                    "The response proposed an invalid session duration constraint."
                ) from exc
            if not 15 <= minutes <= 60:
                raise ModelBusinessValidationError(
                    "The response proposed an invalid session duration constraint."
                )
