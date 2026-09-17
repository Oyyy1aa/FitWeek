"""Versioned deterministic policy parameters for the Phase 1A.1 generator."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerationPolicy:
    version: str
    target_session_minutes: int
    minimum_session_minutes: int
    exercises_per_session: int
    maximum_repair_attempts: int


DEFAULT_GENERATION_POLICY = GenerationPolicy(
    version="phase-1a1-v1",
    target_session_minutes=30,
    minimum_session_minutes=15,
    exercises_per_session=4,
    maximum_repair_attempts=2,
)
