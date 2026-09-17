"""Shared deterministic normalization for structured profile constraints."""

import re
from datetime import datetime, timedelta

from app.domain.common import LocationType
from app.domain.profiles.models import ConstraintType

_TOKEN_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


def normalize_constraint_value(
    constraint_type: ConstraintType,
    raw_value: str,
) -> str:
    value = raw_value.strip()
    if constraint_type in {
        ConstraintType.AVAILABLE_EQUIPMENT,
        ConstraintType.EXCLUDED_FEATURE,
    }:
        normalized = value.lower()
        if _TOKEN_PATTERN.fullmatch(normalized) is None:
            raise ValueError("Constraint value must be a lower_snake_case token.")
        return normalized
    if constraint_type is ConstraintType.ALLOWED_LOCATION:
        return LocationType(value.upper()).value
    if constraint_type is ConstraintType.MAX_SESSION_MINUTES:
        minutes = int(value)
        if not 15 <= minutes <= 60:
            raise ValueError("MAX_SESSION_MINUTES must be between 15 and 60.")
        return str(minutes)
    if constraint_type is ConstraintType.UNAVAILABLE_TIME:
        start, separator, end = value.partition("/")
        if not separator:
            raise ValueError(
                "UNAVAILABLE_TIME must be an ISO-8601 UTC start/end interval."
            )
        start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if (
            start_at.tzinfo is None
            or end_at.tzinfo is None
            or start_at.utcoffset() != timedelta(0)
            or end_at.utcoffset() != timedelta(0)
            or start_at >= end_at
        ):
            raise ValueError("UNAVAILABLE_TIME must be a valid UTC interval.")
        return f"{start_at.isoformat()}/{end_at.isoformat()}"
    if not value:
        raise ValueError("Constraint value must not be blank.")
    return value
