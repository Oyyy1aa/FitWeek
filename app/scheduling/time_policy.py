"""IANA timezone and half-open interval policy using only the standard library."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.common import DomainValidationError


class TimezonePolicy:
    def zone(self, name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise DomainValidationError(
                "timezone must be a valid IANA timezone.", code="INVALID_TIMEZONE"
            ) from exc

    def to_utc(self, value: datetime, timezone: str, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise DomainValidationError(
                f"{field_name} must be timezone-aware.", code="NAIVE_DATETIME"
            )
        zone = self.zone(timezone)
        local_wall = value.replace(tzinfo=None)
        matches: list[datetime] = []
        for fold in (0, 1):
            candidate = local_wall.replace(tzinfo=zone, fold=fold)
            roundtrip = candidate.astimezone(UTC).astimezone(zone)
            if (
                roundtrip.replace(tzinfo=None) == local_wall
                and candidate.utcoffset() == value.utcoffset()
            ):
                matches.append(candidate)
        if not matches:
            possible_offsets = {
                local_wall.replace(tzinfo=zone, fold=fold).utcoffset()
                for fold in (0, 1)
            }
            if value.utcoffset() in possible_offsets:
                raise DomainValidationError(
                    f"{field_name} is a nonexistent local time.",
                    code="NONEXISTENT_LOCAL_TIME",
                )
            raise DomainValidationError(
                f"{field_name} offset does not match timezone {timezone}.",
                code="TIMEZONE_OFFSET_MISMATCH",
            )
        return matches[0].astimezone(UTC)

    def week_bounds(self, week_start: date, timezone: str) -> tuple[datetime, datetime]:
        zone = self.zone(timezone)
        start = datetime.combine(week_start, time.min, tzinfo=zone).astimezone(UTC)
        end = datetime.combine(
            week_start + timedelta(days=7), time.min, tzinfo=zone
        ).astimezone(UTC)
        return start, end


def overlaps(
    start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime
) -> bool:
    """Return overlap for half-open intervals [start, end)."""

    return start_a < end_b and start_b < end_a
