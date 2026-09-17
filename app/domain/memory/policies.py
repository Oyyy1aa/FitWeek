"""Stable Memory classification policy."""

from app.domain.memory.enums import MemoryType

SINGLE_VALUE_MEMORY_TYPES = frozenset(
    {
        MemoryType.PREFERRED_LOCATION,
        MemoryType.PREFERRED_TIME_OF_DAY,
        MemoryType.TRAINING_STYLE_PREFERENCE,
    }
)

MEMORY_POLICY_VERSION = "memory-policy-v1"
