"""Stable Schedule Draft classifications."""

from enum import StrEnum


class CalendarReadMode(StrEnum):
    PROVIDER = "PROVIDER"
    MANUAL_ONLY = "MANUAL_ONLY"
    DISABLED = "DISABLED"


class CalendarVerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    MANUAL_ONLY = "MANUAL_ONLY"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class ScheduleDraftStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    APPLIED = "APPLIED"


class ScheduleDraftOutcome(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class ScheduleDraftSource(StrEnum):
    MODEL = "MODEL"
    DETERMINISTIC_FALLBACK = "DETERMINISTIC_FALLBACK"


class BusyIntervalSource(StrEnum):
    CALENDAR_PROVIDER = "CALENDAR_PROVIDER"
    MANUAL = "MANUAL"
