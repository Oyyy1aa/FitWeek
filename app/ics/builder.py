"""Deterministic safe ICS export from one current confirmed Plan Revision."""

import hashlib
from datetime import UTC, datetime

from app.domain.plans.models import WeeklyPlan
from app.domain.sessions.models import WorkoutSessionStatus
from app.ics.escaping import escape_text
from app.ics.folding import fold_line
from app.ics.validator import validate_ics


class IcsBuilder:
    policy_version = "ics-export-policy-v1"

    @staticmethod
    def _utc(value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    def build(
        self, *, user_id: object, plan: WeeklyPlan, as_of: datetime
    ) -> tuple[bytes, int]:
        sessions = tuple(
            sorted(
                (
                    item
                    for item in plan.sessions
                    if item.status is not WorkoutSessionStatus.CANCELLED
                    and item.scheduled_end > as_of
                ),
                key=lambda item: (item.scheduled_start, str(item.id)),
            )
        )
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//FitWeek//Training Schedule//EN",
            "CALSCALE:GREGORIAN",
        ]
        stamp = self._utc(plan.confirmed_at or plan.updated_at)
        for session in sessions:
            uid_hash = hashlib.sha256(
                (
                    f"fitweek-ics:{user_id}:{plan.series_id}:"
                    f"{plan.revision}:{session.id}"
                ).encode()
            ).hexdigest()
            short_plan = hashlib.sha256(str(plan.series_id).encode()).hexdigest()[:12]
            description = escape_text(
                f"{session.session_type.value}; {session.estimated_minutes} minutes; "
                f"{session.location_type.value}; plan {short_plan}"
            )
            lines.extend(
                (
                    "BEGIN:VEVENT",
                    f"UID:{uid_hash}@fitweek.local",
                    f"DTSTAMP:{stamp}",
                    f"DTSTART:{self._utc(session.scheduled_start)}",
                    f"DTEND:{self._utc(session.scheduled_end)}",
                    "SUMMARY:FitWeek Training",
                    f"DESCRIPTION:{description}",
                    "STATUS:CONFIRMED",
                    "TRANSP:OPAQUE",
                    "END:VEVENT",
                )
            )
        lines.append("END:VCALENDAR")
        physical = [part for line in lines for part in fold_line(line)]
        content = ("\r\n".join(physical) + "\r\n").encode("utf-8")
        validate_ics(content)
        return content, len(sessions)
