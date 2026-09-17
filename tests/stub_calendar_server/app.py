"""Ephemeral local Calendar read/write contract Stub.

Only sanitized event payloads are retained in process memory. Authorization
headers are validated but never stored or returned.
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="FitWeek local Calendar read Stub")

_events: dict[str, dict[str, Any]] = {}
_event_by_operation_key: dict[str, str] = {}
_failures: list[tuple[int, bool]] = []
_read_scenario = "success"
_read_delay_seconds = 0.0
_read_requests = 0
_read_active = 0
_read_max_concurrency = 0
_write_requests = 0
_duplicate_reuses = 0
_read_busy_override: list[dict[str, str]] | None = None


def _authorized(request: Request) -> bool:
    return request.headers.get("authorization") == (
        "Bearer local-calendar-write-test-key"
    )


async def _failure(after_commit: bool) -> JSONResponse | None:
    if _failures and _failures[0][1] is after_commit:
        status_code, _ = _failures.pop(0)
        return JSONResponse({"error": "scripted failure"}, status_code=status_code)
    return None


def _busy() -> list[dict[str, str]]:
    start = datetime(2026, 7, 20, 11, tzinfo=UTC)
    return [
        {
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
            "transparency": "OPAQUE",
            "title": "discard-me",
            "attendees": "discard-me",
        }
    ]


@app.post("/busy")
@app.post("/{scenario}/busy")
@app.post("/freebusy/{scenario}")
async def read_busy(request: Request, scenario: str = "success") -> JSONResponse:
    global _read_active, _read_max_concurrency, _read_requests
    await request.json()
    active_scenario = _read_scenario if scenario == "success" else scenario
    _read_requests += 1
    _read_active += 1
    _read_max_concurrency = max(_read_max_concurrency, _read_active)
    try:
        if _read_delay_seconds:
            await asyncio.sleep(_read_delay_seconds)
        if active_scenario == "rate-limited":
            return JSONResponse({"error": "rate limited"}, status_code=429)
        if active_scenario == "server-error":
            return JSONResponse({"error": "server error"}, status_code=500)
        if active_scenario == "auth-error":
            return JSONResponse({"error": "authentication failed"}, status_code=401)
        if active_scenario == "timeout":
            await asyncio.sleep(0.4)
        if active_scenario == "oversized":
            return JSONResponse({"busy": [], "padding": "x" * 200000})
        if active_scenario == "malformed":
            return JSONResponse({"busy": [{"start": "not-a-time"}]})
        if active_scenario == "empty":
            return JSONResponse({"busy": []})
        if active_scenario == "overlap":
            return JSONResponse({"busy": _busy() + _busy()})
        if _read_busy_override is not None:
            return JSONResponse({"busy": _read_busy_override})
        return JSONResponse({"busy": _busy()})
    finally:
        _read_active -= 1


@app.post("/admin/reset")
async def reset_stub() -> dict[str, int]:
    global _read_active, _read_delay_seconds, _read_max_concurrency
    global _read_requests, _read_scenario
    global _duplicate_reuses, _write_requests
    global _read_busy_override
    _events.clear()
    _event_by_operation_key.clear()
    _failures.clear()
    _read_scenario = "success"
    _read_delay_seconds = 0.0
    _read_requests = 0
    _read_active = 0
    _read_max_concurrency = 0
    _write_requests = 0
    _duplicate_reuses = 0
    _read_busy_override = None
    return {"event_count": 0, "read_requests": 0}


@app.post("/admin/read-control")
async def set_read_control(request: Request) -> dict[str, object]:
    global _read_busy_override, _read_delay_seconds, _read_scenario
    document = await request.json()
    _read_scenario = str(document.get("scenario", "success"))
    _read_delay_seconds = max(0.0, float(document.get("delay_seconds", 0.0)))
    raw_busy = document.get("busy")
    _read_busy_override = raw_busy if isinstance(raw_busy, list) else None
    return {
        "scenario": _read_scenario,
        "delay_seconds": _read_delay_seconds,
        "busy_override": _read_busy_override is not None,
    }


@app.get("/admin/read-stats")
async def read_stats() -> dict[str, object]:
    return {
        "scenario": _read_scenario,
        "delay_seconds": _read_delay_seconds,
        "request_count": _read_requests,
        "active": _read_active,
        "max_concurrency": _read_max_concurrency,
    }


@app.post("/admin/fail-next")
async def fail_next(request: Request) -> dict[str, int | bool]:
    document = await request.json()
    status_code = int(document.get("status_code", 500))
    after_commit = bool(document.get("after_commit", False))
    count = int(document.get("count", 1))
    _failures.extend((status_code, after_commit) for _ in range(count))
    return {"status_code": status_code, "after_commit": after_commit, "count": count}


@app.get("/admin/events")
async def list_events() -> dict[str, object]:
    return {
        "event_count": len(_events),
        "write_request_count": _write_requests,
        "duplicate_reuse_count": _duplicate_reuses,
        "events": [
            {"event_id": event_id, **payload}
            for event_id, payload in sorted(_events.items())
        ],
    }


@app.post("/calendars/{calendar_id}/events")
async def create_event(calendar_id: str, request: Request) -> JSONResponse:
    global _duplicate_reuses, _write_requests
    _write_requests += 1
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    before = await _failure(False)
    if before is not None:
        return before
    payload = await request.json()
    operation_key = request.headers.get("idempotency-key", "")
    if not operation_key:
        return JSONResponse({"error": "missing idempotency key"}, status_code=400)
    event_id = _event_by_operation_key.get(operation_key)
    if event_id is None:
        event_id = f"evt-{hashlib.sha256(operation_key.encode()).hexdigest()[:20]}"
        _event_by_operation_key[operation_key] = event_id
        _events[event_id] = {"calendar_id": calendar_id, **payload}
    elif _events.get(event_id) != {"calendar_id": calendar_id, **payload}:
        return JSONResponse({"error": "idempotency conflict"}, status_code=409)
    else:
        _duplicate_reuses += 1
    after = await _failure(True)
    if after is not None:
        return after
    return JSONResponse({"event_id": event_id})


@app.put("/calendars/{calendar_id}/events/{event_id}")
async def update_event(
    calendar_id: str, event_id: str, request: Request
) -> JSONResponse:
    global _write_requests
    _write_requests += 1
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    before = await _failure(False)
    if before is not None:
        return before
    if event_id not in _events:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.json()
    _events[event_id] = {"calendar_id": calendar_id, **payload}
    return JSONResponse({"event_id": event_id})


@app.delete("/calendars/{calendar_id}/events/{event_id}")
async def delete_event(
    calendar_id: str, event_id: str, request: Request
) -> JSONResponse:
    global _write_requests
    _write_requests += 1
    del calendar_id
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    before = await _failure(False)
    if before is not None:
        return before
    if event_id not in _events:
        return JSONResponse({"error": "not found"}, status_code=404)
    _events.pop(event_id)
    return JSONResponse({"event_id": event_id})
