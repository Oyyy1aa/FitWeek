"""Deterministic Context budget value."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextBudget:
    max_characters: int
    max_memories: int
    max_behavior_items: int

    def __post_init__(self) -> None:
        if self.max_characters < 128:
            raise ValueError("max_characters must be at least 128")
        if self.max_memories < 0 or self.max_behavior_items < 0:
            raise ValueError("item budgets cannot be negative")
