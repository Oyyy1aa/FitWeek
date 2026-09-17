"""Ephemeral local HTTP model stub; it never stores request bodies."""

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="FitWeek local model stub")
_forced_scenario: str | None = None
_scenario_counts: dict[str, int] = {}

_VALID_OUTPUT = {
    "weekly_frequency": 3,
    "max_session_minutes": 30,
    "goals": ["GENERAL_FITNESS"],
    "hard_constraints": [],
    "soft_preferences": [],
    "temporary_constraints": [],
    "equipment": ["resistance_band"],
    "locations": ["HOME"],
    "scope_status": "SUPPORTED",
    "missing_fields": [],
    "memory_candidates": [],
    "explanation_summary": "Structured request parsed for user review.",
}

_PROFILE_APPLY_OUTPUT = {
    **_VALID_OUTPUT,
    "hard_constraints": [
        {
            "constraint_type": "EXCLUDED_FEATURE",
            "value": "jumping",
            "is_hard": True,
            "priority": 100,
            "valid_until": None,
        }
    ],
    "soft_preferences": [{"preference_type": "TIME_OF_DAY", "value": "morning"}],
    "temporary_constraints": [
        {
            "constraint_type": "UNAVAILABLE_TIME",
            "value": "2026-07-22T18:00:00+00:00/2026-07-22T20:00:00+00:00",
            "is_hard": True,
            "priority": 90,
            "valid_until": None,
        }
    ],
    "memory_candidates": [
        {
            "category": "PREFERENCE",
            "value": "prefers morning training",
            "rationale": "The request described a possible long-term preference.",
        }
    ],
}


def _session_designer_output(prompt: str) -> dict[str, object] | None:
    marker = "Frozen structured input:\n"
    end_marker = "\nThe user message field"
    if marker not in prompt or end_marker not in prompt:
        return None
    raw = prompt.split(marker, 1)[1].split(end_marker, 1)[0]
    try:
        context = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(context, dict) or "candidate_set_id" not in context:
        return None
    slots = context.get("slots")
    if not isinstance(slots, list):
        return None
    selections = []
    used: set[str] = set()
    for slot in slots:
        if not isinstance(slot, dict):
            return None
        allowed = slot.get("allowed_exercise_ids")
        slot_id = slot.get("slot_id")
        if not isinstance(allowed, list) or not isinstance(slot_id, str):
            return None
        exercise_id = next(
            (item for item in allowed if isinstance(item, str) and item not in used),
            None,
        )
        if exercise_id is None:
            return None
        used.add(exercise_id)
        selections.append(
            {
                "slot_id": slot_id,
                "exercise_id": exercise_id,
                "sets": None,
                "repetitions": None,
                "duration_seconds": 60,
                "rest_seconds": 0,
            }
        )
    return {
        "template_id": context["template_id"],
        "session_type": context["session_type"],
        "selections": selections,
        "explanation_summary": "Controlled stub selection for contract testing.",
    }


def _schedule_agent_output(prompt: str) -> dict[str, object] | None:
    marker = "Frozen scheduling input:\n"
    end_marker = "\nTask marker:"
    if marker not in prompt or end_marker not in prompt:
        return None
    raw = prompt.split(marker, 1)[1].split(end_marker, 1)[0]
    try:
        context = json.loads(raw)
    except json.JSONDecodeError:
        return None
    sessions = context.get("sessions") if isinstance(context, dict) else None
    slots = context.get("slots") if isinstance(context, dict) else None
    if not isinstance(sessions, list) or not isinstance(slots, list):
        return None
    slot_map = {
        item.get("slot_id"): item
        for item in slots
        if isinstance(item, dict) and isinstance(item.get("slot_id"), str)
    }
    assignments = []
    unresolved = []
    used_slots: set[str] = set()
    used_intervals: list[tuple[str, str]] = []
    for item in sessions:
        if not isinstance(item, dict):
            return None
        session_id = item.get("session_id")
        allowed = item.get("allowed_slot_ids")
        if not isinstance(session_id, str) or not isinstance(allowed, list):
            return None
        selected = None
        for slot_id in allowed:
            slot = slot_map.get(slot_id)
            if not isinstance(slot_id, str) or not isinstance(slot, dict):
                continue
            start, end = slot.get("start"), slot.get("end")
            if not isinstance(start, str) or not isinstance(end, str):
                continue
            if slot_id in used_slots or any(
                start < used_end and used_start < end
                for used_start, used_end in used_intervals
            ):
                continue
            selected = slot_id
            used_intervals.append((start, end))
            break
        if selected is None:
            unresolved.append(session_id)
        else:
            used_slots.add(selected)
            assignments.append({"session_id": session_id, "slot_id": selected})
    return {
        "assignments": assignments,
        "unresolved_session_ids": unresolved,
        "explanation_summary": "Controlled Stub selected only frozen Slot IDs.",
    }


def _recovery_agent_output(prompt: str) -> dict[str, object] | None:
    marker = "Frozen recovery input:\n"
    end_marker = "\nCurrent request marker:"
    if marker not in prompt or end_marker not in prompt:
        return None
    raw = prompt.split(marker, 1)[1].split(end_marker, 1)[0]
    try:
        context = json.loads(raw)
    except json.JSONDecodeError:
        return None
    candidates = (
        context.get("allowed_action_candidates") if isinstance(context, dict) else None
    )
    if not isinstance(candidates, list):
        return None
    preferred = "KEEP_CURRENT_PLAN"
    for request_type, action_type in (
        ("RESCHEDULE_REQUEST", "REQUEST_SESSION_RESCHEDULE"),
        ("REDUCE_FUTURE_LOAD", "REQUEST_SESSION_REDESIGN"),
        ("REPLACE_FUTURE_SESSION", "REQUEST_SESSION_REDESIGN"),
        ("REMOVE_FUTURE_SESSION", "REMOVE_FUTURE_SESSION"),
        ("NEXT_WEEK_REVIEW", "NEXT_WEEK_FREQUENCY_REVIEW"),
    ):
        if request_type in prompt:
            preferred = action_type
            break
    preferred_actions = {preferred}
    if "GENERAL_RECOVERY_REVIEW" in prompt:
        preferred_actions = {
            "REQUEST_SESSION_RESCHEDULE",
            "REQUEST_SESSION_REDESIGN",
        }
    selected = [
        item.get("candidate_id")
        for item in candidates
        if isinstance(item, dict)
        and item.get("action_type") in preferred_actions
        and isinstance(item.get("candidate_id"), str)
    ]
    return {
        "selected_action_candidate_ids": selected,
        "unresolved_session_ids": [],
        "explanation_summary": "Selected only frozen Recovery Candidate IDs.",
    }


def _completion(content: str) -> JSONResponse:
    return JSONResponse(
        {
            "id": "stub-request",
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }
    )


async def _scenario_response(scenario: str) -> JSONResponse:
    if scenario == "profile-apply":
        return _completion(json.dumps(_PROFILE_APPLY_OUTPUT))
    if scenario == "rate-limited":
        return JSONResponse({"error": "rate limited"}, status_code=429)
    if scenario == "server-error":
        return JSONResponse({"error": "server error"}, status_code=500)
    if scenario == "auth-error":
        return JSONResponse({"error": "authentication failed"}, status_code=401)
    if scenario == "timeout":
        await asyncio.sleep(0.4)
        return _completion(json.dumps(_VALID_OUTPUT))
    if scenario == "invalid-json":
        return _completion("this is not json")
    if scenario == "schema-invalid":
        return _completion('{"scope_status":"SUPPORTED"}')
    if scenario == "recovery-unknown-candidate":
        return _completion(
            json.dumps(
                {
                    "selected_action_candidate_ids": [
                        "00000000-0000-0000-0000-000000000001"
                    ],
                    "unresolved_session_ids": [],
                    "explanation_summary": "Unknown controlled candidate test.",
                }
            )
        )
    if scenario == "markdown-json":
        return _completion(f"```json\n{json.dumps(_VALID_OUTPUT)}\n```")
    if scenario == "oversized":
        return _completion("x" * 300000)
    return _completion(json.dumps(_VALID_OUTPUT))


@app.post("/chat/completions")
async def dynamic_completion(request: Request) -> JSONResponse:
    if _forced_scenario is not None:
        return await _scenario_response(_forced_scenario)
    document = await request.json()
    scenario = "success"
    if isinstance(document, dict):
        messages = document.get("messages")
        if isinstance(messages, list):
            joined = " ".join(
                str(message.get("content", ""))
                for message in messages
                if isinstance(message, dict)
            )
            session_output = _session_designer_output(joined)
            if session_output is not None:
                return _completion(json.dumps(session_output))
            schedule_output = _schedule_agent_output(joined)
            if schedule_output is not None:
                return _completion(json.dumps(schedule_output))
            recovery_output = _recovery_agent_output(joined)
            if recovery_output is not None:
                return _completion(json.dumps(recovery_output))
            for candidate in (
                "rate-limited",
                "server-error",
                "auth-error",
                "timeout",
                "invalid-json",
                "schema-invalid",
                "markdown-json",
                "oversized",
                "profile-apply",
            ):
                if f"stub:{candidate}" in joined:
                    scenario = candidate
                    break
    return await _scenario_response(scenario)


@app.post("/admin/scenario/{scenario}")
async def force_scenario(scenario: str) -> dict[str, str]:
    """Test-only process-local fault control; request bodies are never retained."""

    global _forced_scenario
    allowed = {
        "success",
        "rate-limited",
        "server-error",
        "timeout",
        "invalid-json",
        "schema-invalid",
        "recovery-unknown-candidate",
    }
    if scenario not in allowed:
        return {"status": "rejected"}
    _forced_scenario = None if scenario == "success" else scenario
    return {"status": "ok", "scenario": scenario}


@app.post("/{scenario}")
@app.post("/{scenario}/chat/completions")
async def named_completion(scenario: str) -> JSONResponse:
    _scenario_counts[scenario] = _scenario_counts.get(scenario, 0) + 1
    return await _scenario_response(scenario)


@app.get("/admin/count/{scenario}")
async def scenario_count(scenario: str) -> dict[str, int]:
    """Expose only a test call count; request bodies are never retained."""

    return {"count": _scenario_counts.get(scenario, 0)}
