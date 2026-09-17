"""Safe deterministic template fallback over the frozen Candidate Set."""

from app.domain.session_design.models import (
    ExerciseCandidateSet,
    SessionDesignerOutput,
    SessionDesignerSelection,
)
from app.domain.session_design.templates import SessionTemplate


class DeterministicSessionFallback:
    def build(
        self, candidate_set: ExerciseCandidateSet, template: SessionTemplate
    ) -> SessionDesignerOutput:
        used: set[str] = set()
        selections: list[SessionDesignerSelection] = []
        for slot in candidate_set.slots:
            exercise_id = next(
                (item for item in slot.exercise_ids if item not in used),
                None,
            )
            if exercise_id is None:
                raise ValueError("Candidate Set has insufficient exercise variety.")
            used.add(exercise_id)
            selections.append(
                SessionDesignerSelection(
                    slot_id=slot.slot_id,
                    exercise_id=exercise_id,
                    duration_seconds=60,
                    rest_seconds=15 if slot.role.value == "MAIN" else 0,
                )
            )
        return SessionDesignerOutput(
            template_id=template.id,
            session_type=template.session_type,
            selections=tuple(selections),
            explanation_summary=(
                "A deterministic controlled template was used for review."
            ),
        )
