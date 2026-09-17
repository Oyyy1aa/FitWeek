"""Build privacy-minimized Calendar payloads from controlled Plan fields."""

import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.calendar_operations.models import CalendarEventPayload
from app.domain.plans.models import WeeklyPlan
from app.domain.sessions.models import WorkoutSession


class CalendarPayloadPolicy:
    version = "calendar-payload-v1"

    def build(
        self, *, user_id: UUID, plan: WeeklyPlan, session: WorkoutSession, timezone: str
    ) -> CalendarEventPayload:
        stable_uid = str(
            uuid5(
                NAMESPACE_URL,
                f"fitweek:calendar:{user_id}:{plan.series_id}:{session.id}",
            )
        )
        summary = "FitWeek Training"
        description = (
            f"{session.session_type.value}; {session.estimated_minutes} minutes; "
            f"{session.location_type.value}"
        )
        canonical = {
            "session_id": str(session.id),
            "stable_uid": stable_uid,
            "summary": summary,
            "description": description,
            "start": session.scheduled_start.isoformat(),
            "end": session.scheduled_end.isoformat(),
            "timezone": timezone,
            "transparency": "OPAQUE",
            "policy": self.version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return CalendarEventPayload(
            session_id=session.id,
            stable_uid=stable_uid,
            summary=summary,
            description=description,
            start=session.scheduled_start,
            end=session.scheduled_end,
            timezone=timezone,
            transparency="OPAQUE",
            payload_fingerprint=fingerprint,
        )
