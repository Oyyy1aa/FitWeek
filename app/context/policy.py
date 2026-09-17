"""Priority, relevance, and conflict rules for confirmed Memory."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.domain.context.contracts import ContextContract
from app.domain.context.models import ContextConflict
from app.domain.memory.enums import MemoryType
from app.domain.memory.models import UserMemory
from app.domain.profiles.models import ConstraintType, UserConstraint
from app.memory.normalization import normalize_key, normalize_value

_TASK_ALIASES = {
    MemoryType.PREFERRED_LOCATION: "preferred_location",
    MemoryType.PREFERRED_TIME_OF_DAY: "preferred_time_of_day",
    MemoryType.DISLIKED_ACTIVITY: "disliked_activity",
    MemoryType.PREFERRED_EQUIPMENT: "preferred_equipment",
    MemoryType.TRAINING_STYLE_PREFERENCE: "training_style",
}


class ContextPriorityPolicy:
    def order(
        self, memories: Sequence[UserMemory], contract: ContextContract
    ) -> tuple[UserMemory, ...]:
        tier = {item: index for index, item in enumerate(contract.allowed_memory_types)}
        return tuple(
            sorted(
                memories,
                key=lambda item: (
                    tier[item.memory_type],
                    -(item.confirmed_at.timestamp() if item.confirmed_at else 0),
                    item.memory_type.value,
                    item.key,
                    str(item.id),
                ),
            )
        )

    def conflict(
        self,
        *,
        memory: UserMemory,
        current_task: Mapping[str, str],
        constraints: Sequence[UserConstraint],
        profile_values: Mapping[str, str],
    ) -> ContextConflict | None:
        task = {
            normalize_key(key): normalize_value(value)
            for key, value in current_task.items()
        }
        alias = _TASK_ALIASES[memory.memory_type]
        for task_key in (memory.key, alias):
            task_value = task.get(task_key)
            if task_value is not None and task_value != memory.normalized_value:
                return ContextConflict(
                    higher_priority_source="CURRENT_TASK",
                    lower_priority_source="MEMORY",
                    key=memory.key,
                    resolution="Memory shadowed by the current structured request.",
                )

        hard_values: dict[ConstraintType, set[str]] = {}
        for constraint in constraints:
            if constraint.is_hard:
                hard_values.setdefault(constraint.constraint_type, set()).add(
                    normalize_value(constraint.constraint_value)
                )
        if (
            memory.memory_type is MemoryType.PREFERRED_LOCATION
            and hard_values.get(ConstraintType.ALLOWED_LOCATION)
            and memory.normalized_value
            not in hard_values[ConstraintType.ALLOWED_LOCATION]
        ):
            return ContextConflict(
                higher_priority_source="HARD_CONSTRAINTS",
                lower_priority_source="MEMORY",
                key=memory.key,
                resolution="Location preference excluded by allowed locations.",
            )
        if (
            memory.memory_type is MemoryType.PREFERRED_EQUIPMENT
            and hard_values.get(ConstraintType.AVAILABLE_EQUIPMENT)
            and memory.normalized_value
            not in hard_values[ConstraintType.AVAILABLE_EQUIPMENT]
        ):
            return ContextConflict(
                higher_priority_source="HARD_CONSTRAINTS",
                lower_priority_source="MEMORY",
                key=memory.key,
                resolution="Equipment preference excluded by available equipment.",
            )
        if (
            memory.memory_type is not MemoryType.DISLIKED_ACTIVITY
            and memory.normalized_value
            in hard_values.get(ConstraintType.EXCLUDED_FEATURE, set())
        ):
            return ContextConflict(
                higher_priority_source="HARD_CONSTRAINTS",
                lower_priority_source="MEMORY",
                key=memory.key,
                resolution="Preference excluded by an explicit feature constraint.",
            )
        profile_value = profile_values.get(memory.key)
        if (
            profile_value is not None
            and normalize_value(profile_value) != memory.normalized_value
        ):
            return ContextConflict(
                higher_priority_source="PROFILE",
                lower_priority_source="MEMORY",
                key=memory.key,
                resolution="Memory shadowed by the current formal Profile.",
            )
        return None
