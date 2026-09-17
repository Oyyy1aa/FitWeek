"""Safe soft preferences derived from a frozen planning Context."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanningContextPreferences:
    preferred_locations: tuple[str, ...] = ()
    preferred_times_of_day: tuple[str, ...] = ()
    preferred_equipment: tuple[str, ...] = ()
    disliked_activities: tuple[str, ...] = ()
    training_styles: tuple[str, ...] = ()
