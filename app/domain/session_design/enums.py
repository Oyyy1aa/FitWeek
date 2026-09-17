"""Stable identifiers for the controlled Session Designer contract."""

from enum import StrEnum


class SessionTemplateId(StrEnum):
    FULL_BODY_BASIC = "FULL_BODY_BASIC"
    UPPER_BODY_BASIC = "UPPER_BODY_BASIC"
    LOWER_BODY_BASIC = "LOWER_BODY_BASIC"
    LOW_IMPACT_CARDIO = "LOW_IMPACT_CARDIO"
    MOBILITY_RECOVERY = "MOBILITY_RECOVERY"
    MIXED_HOME = "MIXED_HOME"


class SessionExerciseRole(StrEnum):
    WARMUP = "WARMUP"
    MAIN = "MAIN"
    COOLDOWN = "COOLDOWN"


class SessionDesignSource(StrEnum):
    MODEL = "MODEL"
    TEMPLATE_FALLBACK = "TEMPLATE_FALLBACK"


class SessionDesignDraftStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    APPLIED = "APPLIED"
