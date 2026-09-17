"""Safe structured evidence projection; free-text notes are never accepted."""

import hashlib
import json
from datetime import datetime

from app.domain.behavior.models import BehaviorEvidenceReference
from app.domain.checkins.models import WorkoutCheckIn
from app.domain.sessions.models import WorkoutSession


def time_bucket(value: datetime) -> str:
    hour = value.hour
    if hour < 6:
        return "NIGHT"
    if hour < 12:
        return "MORNING"
    if hour < 18:
        return "AFTERNOON"
    return "EVENING"


def build_evidence(
    *,
    check_in: WorkoutCheckIn,
    session: WorkoutSession,
    root_plan_id: object,
    timezone: object,
) -> BehaviorEvidenceReference:
    local = session.scheduled_start.astimezone(timezone)  # type: ignore[arg-type]
    payload = {
        "checkin_id": str(check_in.id),
        "logical_session_id": str(session.id),
        "root_plan_id": str(root_plan_id),
        "revision": check_in.plan_revision,
        "scheduled_at": session.scheduled_start.isoformat(),
        "status": check_in.status.value,
        "rpe": check_in.perceived_effort,
        "occurred_at": check_in.occurred_at.isoformat(),
        "weekday": local.strftime("%A").upper(),
        "time_bucket": time_bucket(local),
        "location": session.location_type.value,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return BehaviorEvidenceReference(
        checkin_id=check_in.id,
        logical_session_id=session.id,
        root_plan_id=root_plan_id,  # type: ignore[arg-type]
        plan_revision=check_in.plan_revision,
        scheduled_at_utc=session.scheduled_start,
        status=check_in.status,
        reported_rpe=check_in.perceived_effort,
        occurred_at=check_in.occurred_at,
        scheduled_weekday=payload["weekday"],  # type: ignore[arg-type]
        scheduled_time_bucket=payload["time_bucket"],  # type: ignore[arg-type]
        location=payload["location"],  # type: ignore[arg-type]
        fingerprint=digest,
    )
