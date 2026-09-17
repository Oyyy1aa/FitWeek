"""HTTP payload helpers for the deterministic Phase 1A API tests."""

from datetime import UTC, datetime, timedelta


def profile_payload(
    *,
    scope_confirmed: bool = True,
    weekly_frequency: int = 2,
    experience_level: str = "BEGINNER",
    primary_goal: str = "GENERAL_FITNESS",
) -> dict[str, object]:
    return {
        "experience_level": experience_level,
        "weekly_frequency": weekly_frequency,
        "max_session_minutes": 45,
        "primary_goal": primary_goal,
        "scope_confirmed": scope_confirmed,
    }


def generation_payload(
    *,
    slot_count: int = 2,
    location: str = "HOME",
) -> dict[str, object]:
    availability = []
    for offset in range(slot_count):
        start = datetime(2026, 7, 20 + offset * 2, 10, tzinfo=UTC)
        availability.append(
            {
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=60)).isoformat(),
                "location_type": location,
            }
        )
    return {
        "week_start": "2026-07-20",
        "availability_slots": availability,
        "preferred_locations": [location],
        "preferred_session_types": [],
    }


def plan_payload(
    exercise_id: str = "bodyweight_squat",
    *,
    revision: int = 1,
) -> dict[str, object]:
    sessions: list[dict[str, object]] = []
    for offset in (0, 2):
        start = datetime(2026, 7, 20 + offset, 10, tzinfo=UTC)
        sessions.append(
            {
                "scheduled_start": start.isoformat(),
                "scheduled_end": (start + timedelta(minutes=30)).isoformat(),
                "location_type": "HOME",
                "session_type": "MIXED",
                "estimated_minutes": 30,
                "target_difficulty": 4,
                "exercises": [
                    {
                        "exercise_id": exercise_id,
                        "sequence_no": 1,
                        "sets": 2,
                        "repetitions": 8,
                        "duration_seconds": None,
                        "rest_seconds": 30,
                    }
                ],
            }
        )
    return {
        "week_start": "2026-07-20",
        "revision": revision,
        "sessions": sessions,
    }
