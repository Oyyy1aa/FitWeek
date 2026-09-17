"""Explicit template registry and deterministic selection policy."""

from app.domain.common import LocationType
from app.domain.profiles.models import ExperienceLevel, FitnessGoal
from app.domain.session_design.enums import SessionExerciseRole, SessionTemplateId
from app.domain.session_design.templates import SessionTemplate, SessionTemplateSlot
from app.domain.sessions.models import SessionType


def _slot(
    slot_id: str,
    role: SessionExerciseRole,
    *patterns: str,
    tags: tuple[str, ...] = (),
) -> SessionTemplateSlot:
    return SessionTemplateSlot(
        slot_id=slot_id,
        role=role,
        required_patterns=frozenset(patterns),
        preferred_tags=frozenset(tags),
    )


def _template(
    *,
    id: SessionTemplateId,
    session_type: SessionType,
    slots: tuple[SessionTemplateSlot, ...],
    goals: tuple[FitnessGoal, ...] = tuple(FitnessGoal),
    locations: tuple[LocationType, ...] = tuple(LocationType),
) -> SessionTemplate:
    return SessionTemplate(
        id=id,
        session_type=session_type,
        supported_goals=goals,
        supported_levels=tuple(ExperienceLevel),
        supported_locations=locations,
        min_duration_minutes=15,
        max_duration_minutes=60,
        slots=slots,
    )


TEMPLATES = (
    _template(
        id=SessionTemplateId.FULL_BODY_BASIC,
        session_type=SessionType.STRENGTH,
        slots=(
            _slot("warmup", SessionExerciseRole.WARMUP, "mobility", "locomotion"),
            _slot("lower", SessionExerciseRole.MAIN, "squat", "hinge"),
            _slot(
                "upper", SessionExerciseRole.MAIN, "horizontal_push", "horizontal_pull"
            ),
            _slot("cooldown", SessionExerciseRole.COOLDOWN, "stretch", "mobility"),
        ),
    ),
    _template(
        id=SessionTemplateId.UPPER_BODY_BASIC,
        session_type=SessionType.STRENGTH,
        slots=(
            _slot("warmup", SessionExerciseRole.WARMUP, "mobility"),
            _slot("push", SessionExerciseRole.MAIN, "horizontal_push", "vertical_push"),
            _slot("pull", SessionExerciseRole.MAIN, "horizontal_pull"),
            _slot("cooldown", SessionExerciseRole.COOLDOWN, "stretch", "mobility"),
        ),
    ),
    _template(
        id=SessionTemplateId.LOWER_BODY_BASIC,
        session_type=SessionType.STRENGTH,
        slots=(
            _slot("warmup", SessionExerciseRole.WARMUP, "mobility", "locomotion"),
            _slot("squat", SessionExerciseRole.MAIN, "squat"),
            _slot("hinge", SessionExerciseRole.MAIN, "hinge", "calf_raise"),
            _slot("cooldown", SessionExerciseRole.COOLDOWN, "stretch", "mobility"),
        ),
    ),
    _template(
        id=SessionTemplateId.LOW_IMPACT_CARDIO,
        session_type=SessionType.CARDIO,
        slots=(
            _slot("warmup", SessionExerciseRole.WARMUP, "mobility"),
            _slot(
                "cardio_one",
                SessionExerciseRole.MAIN,
                "locomotion",
                tags=("low_impact",),
            ),
            _slot(
                "cardio_two",
                SessionExerciseRole.MAIN,
                "lateral_locomotion",
                "locomotion",
                tags=("low_impact",),
            ),
            _slot("cooldown", SessionExerciseRole.COOLDOWN, "stretch", "mobility"),
        ),
    ),
    _template(
        id=SessionTemplateId.MOBILITY_RECOVERY,
        session_type=SessionType.MOBILITY,
        slots=(
            _slot("warmup", SessionExerciseRole.WARMUP, "locomotion"),
            _slot("mobility", SessionExerciseRole.MAIN, "mobility"),
            _slot("stretch", SessionExerciseRole.COOLDOWN, "stretch"),
        ),
    ),
    _template(
        id=SessionTemplateId.MIXED_HOME,
        session_type=SessionType.MIXED,
        locations=(LocationType.HOME,),
        slots=(
            _slot("warmup", SessionExerciseRole.WARMUP, "mobility", "locomotion"),
            _slot(
                "strength",
                SessionExerciseRole.MAIN,
                "squat",
                "horizontal_push",
                "horizontal_pull",
            ),
            _slot(
                "cardio", SessionExerciseRole.MAIN, "locomotion", "lateral_locomotion"
            ),
            _slot("cooldown", SessionExerciseRole.COOLDOWN, "stretch", "mobility"),
        ),
    ),
)


class SessionTemplateRegistry:
    def __init__(self) -> None:
        self._templates = {item.id: item for item in TEMPLATES}

    def get(self, template_id: SessionTemplateId) -> SessionTemplate:
        return self._templates[template_id]

    def select(
        self,
        *,
        goal: FitnessGoal,
        location: LocationType,
        preferred_type: SessionType | None,
        requested: SessionTemplateId | None,
        experience_level: ExperienceLevel,
        target_duration_minutes: int,
    ) -> SessionTemplate:
        if requested is not None:
            selected = self.get(requested)
            if not self._compatible(
                selected,
                goal=goal,
                location=location,
                experience_level=experience_level,
                duration=target_duration_minutes,
            ):
                raise ValueError("The requested Session Template is incompatible.")
            return selected
        if preferred_type is SessionType.MOBILITY or goal is FitnessGoal.MOBILITY:
            chosen = SessionTemplateId.MOBILITY_RECOVERY
        elif (
            preferred_type is SessionType.CARDIO
            or goal is FitnessGoal.LOW_IMPACT_CARDIO
        ):
            chosen = SessionTemplateId.LOW_IMPACT_CARDIO
        elif location is LocationType.HOME and goal is FitnessGoal.MIXED:
            chosen = SessionTemplateId.MIXED_HOME
        elif goal is FitnessGoal.BASIC_STRENGTH:
            chosen = SessionTemplateId.FULL_BODY_BASIC
        else:
            chosen = SessionTemplateId.FULL_BODY_BASIC
        selected = self.get(chosen)
        if self._compatible(
            selected,
            goal=goal,
            location=location,
            experience_level=experience_level,
            duration=target_duration_minutes,
        ):
            return selected
        compatible = sorted(
            (
                item
                for item in self._templates.values()
                if self._compatible(
                    item,
                    goal=goal,
                    location=location,
                    experience_level=experience_level,
                    duration=target_duration_minutes,
                )
            ),
            key=lambda item: item.id.value,
        )
        if not compatible:
            raise ValueError("No compatible Session Template exists.")
        return compatible[0]

    @staticmethod
    def _compatible(
        template: SessionTemplate,
        *,
        goal: FitnessGoal,
        location: LocationType,
        experience_level: ExperienceLevel,
        duration: int,
    ) -> bool:
        return (
            goal in template.supported_goals
            and location in template.supported_locations
            and experience_level in template.supported_levels
            and template.min_duration_minutes
            <= duration
            <= template.max_duration_minutes
        )
