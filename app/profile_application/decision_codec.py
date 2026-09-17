"""Safe JSON codec for user-authored Profile Draft decisions in checkpoints."""

from datetime import datetime
from typing import cast

from app.domain.common import LocationType
from app.domain.orchestration.models import JsonObject, JsonValue
from app.domain.profile_agent.apply_models import ProfileDraftApplyDecision
from app.domain.profiles.models import ExperienceLevel, FitnessGoal


def decision_to_payload(decision: ProfileDraftApplyDecision) -> JsonObject:
    return {
        "client_request_id": decision.client_request_id,
        "expected_draft_version": decision.expected_draft_version,
        "expected_profile_version": decision.expected_profile_version,
        "accept_weekly_frequency": decision.accept_weekly_frequency,
        "accept_max_session_minutes": decision.accept_max_session_minutes,
        "selected_primary_goal": (
            decision.selected_primary_goal.value
            if decision.selected_primary_goal is not None
            else None
        ),
        "accepted_equipment": list(decision.accepted_equipment),
        "accepted_locations": [item.value for item in decision.accepted_locations],
        "accepted_hard_constraint_indexes": list(
            decision.accepted_hard_constraint_indexes
        ),
        "accepted_temporary_constraint_indexes": list(
            decision.accepted_temporary_constraint_indexes
        ),
        "temporary_constraint_expirations": {
            str(index): expiration.isoformat()
            for index, expiration in decision.temporary_constraint_expirations.items()
        },
        "confirmed_experience_level": (
            decision.confirmed_experience_level.value
            if decision.confirmed_experience_level is not None
            else None
        ),
        "confirm_scope": decision.confirm_scope,
    }


def decision_from_payload(payload: JsonObject) -> ProfileDraftApplyDecision:
    try:
        raw_expirations = cast(
            dict[str, JsonValue], payload["temporary_constraint_expirations"]
        )
        selected_goal = payload["selected_primary_goal"]
        experience = payload["confirmed_experience_level"]
        return ProfileDraftApplyDecision(
            client_request_id=cast(str, payload["client_request_id"]),
            expected_draft_version=cast(int, payload["expected_draft_version"]),
            expected_profile_version=cast(
                int | None,
                payload["expected_profile_version"],
            ),
            accept_weekly_frequency=cast(
                bool,
                payload["accept_weekly_frequency"],
            ),
            accept_max_session_minutes=cast(
                bool,
                payload["accept_max_session_minutes"],
            ),
            selected_primary_goal=(
                FitnessGoal(cast(str, selected_goal))
                if selected_goal is not None
                else None
            ),
            accepted_equipment=tuple(
                cast(str, item)
                for item in cast(list[JsonValue], payload["accepted_equipment"])
            ),
            accepted_locations=tuple(
                LocationType(cast(str, item))
                for item in cast(list[JsonValue], payload["accepted_locations"])
            ),
            accepted_hard_constraint_indexes=tuple(
                cast(int, item)
                for item in cast(
                    list[JsonValue],
                    payload["accepted_hard_constraint_indexes"],
                )
            ),
            accepted_temporary_constraint_indexes=tuple(
                cast(int, item)
                for item in cast(
                    list[JsonValue],
                    payload["accepted_temporary_constraint_indexes"],
                )
            ),
            temporary_constraint_expirations={
                int(index): datetime.fromisoformat(cast(str, value))
                for index, value in raw_expirations.items()
            },
            confirmed_experience_level=(
                ExperienceLevel(cast(str, experience))
                if experience is not None
                else None
            ),
            confirm_scope=cast(bool, payload["confirm_scope"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Profile Draft Apply Decision payload is invalid.") from exc
