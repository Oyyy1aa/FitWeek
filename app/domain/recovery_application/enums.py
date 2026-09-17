"""Stable Recovery application classifications."""

from enum import StrEnum


class RecoveryApplicationOutcome(StrEnum):
    NO_CHANGE = "NO_CHANGE"
    PLAN_REVISION_CREATED = "PLAN_REVISION_CREATED"
    NEXT_WEEK_REVIEW_CREATED = "NEXT_WEEK_REVIEW_CREATED"


class RecoveryActionResolutionStatus(StrEnum):
    READY = "READY"
    REQUIRES_SUBDRAFT_REVIEW = "REQUIRES_SUBDRAFT_REVIEW"
