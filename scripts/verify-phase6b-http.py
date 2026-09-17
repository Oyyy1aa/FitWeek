"""Run the Phase 6B localhost HTTP contract against already-started services."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

FITWEEK = "http://127.0.0.1:8000"
WRITE_STUB = "http://127.0.0.1:8013"


def _profile_payload() -> dict[str, object]:
    return {
        "experience_level": "BEGINNER",
        "weekly_frequency": 2,
        "max_session_minutes": 45,
        "primary_goal": "GENERAL_FITNESS",
        "scope_confirmed": True,
    }


def _generation_payload(week_start: date = date(2026, 7, 20)) -> dict[str, object]:
    starts = tuple(
        datetime.combine(
            week_start + timedelta(days=offset), datetime.min.time(), UTC
        ).replace(hour=10)
        for offset in (0, 2)
    )
    return {
        "week_start": week_start.isoformat(),
        "availability_slots": [
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "location_type": "HOME",
            }
            for start in starts
        ],
        "preferred_locations": ["HOME"],
        "preferred_session_types": [],
    }


def _request(
    client: httpx.Client, method: str, path: str, *, expected: int, **kwargs: Any
) -> httpx.Response:
    if client.base_url.port == 8000:
        path = "/health/live" if path == "/../health/live" else f"/api/v1{path}"
    response = client.request(method, path, **kwargs)
    if response.status_code != expected:
        raise AssertionError(
            f"{method} {path}: expected {expected}, got "
            f"{response.status_code}: {response.text}"
        )
    return response


def _wait_run(client: httpx.Client, path: str, statuses: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 8
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _request(client, "GET", path, expected=200).json()
        if latest["status"] in statuses:
            return latest
        time.sleep(0.03)
    raise AssertionError(f"Run timeout at {path}: {latest}")


def _schedule_request(
    plan: dict[str, Any], request_id: str, starts: list[datetime]
) -> dict[str, Any]:
    return {
        "client_request_id": request_id,
        "root_plan_id": plan.get("root_plan_id") or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "timezone": "UTC",
        "availability_windows": [
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=2)).isoformat(),
                "location": session["location_type"],
            }
            for start, session in zip(starts, plan["sessions"], strict=True)
        ],
        "manual_busy_windows": [],
    }


def _schedule_revision(
    client: httpx.Client,
    plan: dict[str, Any],
    request_id: str,
    starts: list[datetime],
) -> tuple[dict[str, Any], dict[str, Any]]:
    draft = _request(
        client,
        "POST",
        "/schedule-drafts",
        expected=201,
        json=_schedule_request(plan, request_id, starts),
    ).json()
    accepted = _request(
        client,
        "POST",
        f"/schedule-drafts/{draft['id']}/accept",
        expected=200,
        json={"expected_version": draft["version"]},
    ).json()
    apply_payload = {
        "client_request_id": f"{request_id}-apply",
        "expected_draft_version": accepted["version"],
        "root_plan_id": plan.get("root_plan_id") or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
    }
    preview = _request(
        client,
        "POST",
        f"/schedule-drafts/{draft['id']}/apply-preview",
        expected=200,
        json=apply_payload,
    ).json()
    assert preview["passed"] is True
    run = _request(
        client,
        "POST",
        "/schedule-application-runs",
        expected=202,
        json={
            "client_request_id": f"{request_id}-run",
            "draft_id": draft["id"],
            "expected_draft_version": accepted["version"],
            "root_plan_id": plan.get("root_plan_id") or plan["id"],
            "source_revision": plan["revision"],
            "expected_plan_version": plan["version"],
        },
    ).json()
    waiting = _wait_run(
        client,
        f"/schedule-application-runs/{run['id']}",
        {"WAITING_CONFIRMATION", "FAILED_PERMANENT"},
    )
    assert waiting["status"] == "WAITING_CONFIRMATION", waiting
    revision_no = plan["revision"] + 1
    root = plan.get("root_plan_id") or plan["id"]
    revision = _request(
        client, "GET", f"/plans/{root}/revisions/{revision_no}", expected=200
    ).json()["plan"]
    _request(
        client,
        "POST",
        f"/schedule-application-runs/{run['id']}/confirm",
        expected=202,
        json={
            "expected_revision": revision_no,
            "expected_plan_version": revision["version"],
        },
    )
    completed = _wait_run(
        client,
        f"/schedule-application-runs/{run['id']}",
        {"COMPLETED", "FAILED_PERMANENT"},
    )
    assert completed["status"] == "COMPLETED", completed
    confirmed = _request(
        client, "GET", f"/plans/{root}/revisions/{revision_no}", expected=200
    ).json()["plan"]
    assert confirmed["status"] == "CONFIRMED"
    stored_draft = _request(
        client, "GET", f"/schedule-drafts/{draft['id']}", expected=200
    ).json()
    assert stored_draft["status"] == "APPLIED"
    return confirmed, stored_draft


def _sync(
    client: httpx.Client,
    plan: dict[str, Any],
    request_id: str,
    *,
    via_run: bool,
) -> dict[str, Any]:
    root = plan.get("root_plan_id") or plan["id"]
    draft = _request(
        client,
        "POST",
        f"/plans/{root}/revisions/{plan['revision']}/calendar-operation-drafts",
        expected=201,
        json={
            "client_request_id": request_id,
            "expected_plan_version": plan["version"],
            "provider": "http",
            "calendar_id": "primary",
        },
    ).json()
    approved = _request(
        client,
        "POST",
        f"/calendar-operation-drafts/{draft['id']}/approve",
        expected=200,
        json={"expected_version": draft["version"]},
    ).json()
    if not via_run:
        return _request(
            client,
            "POST",
            f"/calendar-operation-drafts/{draft['id']}/execute",
            expected=200,
        ).json()
    run = _request(
        client,
        "POST",
        "/calendar-operation-runs",
        expected=202,
        json={"client_request_id": f"{request_id}-run", "draft_id": approved["id"]},
    ).json()
    completed = _wait_run(
        client,
        f"/calendar-operation-runs/{run['id']}",
        {"COMPLETED", "FAILED_PERMANENT"},
    )
    assert completed["status"] == "COMPLETED", completed
    return _request(
        client,
        "GET",
        f"/calendar-operation-drafts/{draft['id']}",
        expected=200,
    ).json()


def main() -> None:
    with (
        httpx.Client(base_url=FITWEEK, timeout=10) as client,
        httpx.Client(base_url=WRITE_STUB, timeout=5) as stub,
    ):
        _request(stub, "POST", "/admin/reset", expected=200)
        live = _request(client, "GET", "/../health/live", expected=200).json()
        _request(client, "PUT", "/profiles/me", expected=200, json=_profile_payload())
        generated = _request(
            client, "POST", "/plans/generate", expected=201, json=_generation_payload()
        ).json()["plan"]
        revision1 = _request(
            client,
            "POST",
            f"/plans/{generated['id']}/confirm",
            expected=200,
            json={"expected_version": generated["version"]},
        ).json()
        starts2 = [
            datetime(2026, 7, 20, 12, tzinfo=UTC),
            datetime(2026, 7, 22, 12, tzinfo=UTC),
        ]
        revision2, draft2 = _schedule_revision(
            client, revision1, "http-schedule-r2", starts2
        )
        assert all(
            before["exercises"] == after["exercises"]
            and before["estimated_minutes"] == after["estimated_minutes"]
            and before["location_type"] == after["location_type"]
            and before["session_type"] == after["session_type"]
            for before, after in zip(
                revision1["sessions"], revision2["sessions"], strict=True
            )
        )

        root = revision2.get("root_plan_id") or revision2["id"]
        export_payload = {
            "client_request_id": "http-ics-r2",
            "expected_plan_version": revision2["version"],
        }
        export1 = _request(
            client,
            "POST",
            f"/plans/{root}/revisions/2/ics-export",
            expected=201,
            json=export_payload,
        ).json()
        export2 = _request(
            client,
            "POST",
            f"/plans/{root}/revisions/2/ics-export",
            expected=200,
            json=export_payload,
        ).json()
        content = _request(
            client,
            "GET",
            f"/ics-exports/{export1['id']}/download",
            expected=200,
        ).content
        assert hashlib.sha256(content).hexdigest() == export1["content_sha256"]
        assert export1["content_sha256"] == export2["content_sha256"]
        assert content.count(b"BEGIN:VEVENT") == len(revision2["sessions"])
        assert b"\n" not in content.replace(b"\r\n", b"")
        assert stub.get("/admin/events").json()["event_count"] == 0

        first_sync = _sync(client, revision2, "http-calendar-r2", via_run=True)
        assert first_sync["status"] == "SUCCEEDED"
        assert {item["operation_type"] for item in first_sync["items"]} == {"CREATE"}
        initial_event_count = stub.get("/admin/events").json()["event_count"]
        assert initial_event_count == len(revision2["sessions"])

        starts3 = [
            datetime.fromisoformat(revision2["sessions"][0]["scheduled_start"]),
            datetime.fromisoformat(revision2["sessions"][1]["scheduled_start"])
            + timedelta(hours=2),
        ]
        revision3, _ = _schedule_revision(
            client, revision2, "http-schedule-r3", starts3
        )
        reconcile = _sync(client, revision3, "http-calendar-r3", via_run=True)
        operations = {item["operation_type"] for item in reconcile["items"]}
        assert "KEEP" in operations and "UPDATE" in operations, operations
        assert stub.get("/admin/events").json()["event_count"] == initial_event_count

        next_generated = _request(
            client,
            "POST",
            "/plans/generate",
            expected=201,
            json=_generation_payload(date(2026, 7, 27)),
        ).json()["plan"]
        next_confirmed = _request(
            client,
            "POST",
            f"/plans/{next_generated['id']}/confirm",
            expected=200,
            json={"expected_version": next_generated["version"]},
        ).json()
        _request(
            stub,
            "POST",
            "/admin/fail-next",
            expected=200,
            json={"status_code": 500, "count": 1, "after_commit": True},
        )
        lost_response = _sync(
            client, next_confirmed, "http-calendar-response-loss", via_run=False
        )
        assert lost_response["status"] == "PARTIALLY_SUCCEEDED"
        count_after_lost_response = stub.get("/admin/events").json()["event_count"]
        recovered_loss = _request(
            client,
            "POST",
            f"/calendar-operation-drafts/{lost_response['id']}/retry",
            expected=200,
        ).json()
        assert recovered_loss["status"] == "SUCCEEDED"
        assert stub.get("/admin/events").json()["event_count"] == (
            count_after_lost_response
        )

        starts4 = [
            datetime.fromisoformat(item["scheduled_start"]) + timedelta(hours=1)
            for item in revision3["sessions"]
        ]
        revision4, _ = _schedule_revision(
            client, revision3, "http-schedule-r4", starts4
        )
        _request(
            stub,
            "POST",
            "/admin/fail-next",
            expected=200,
            json={"status_code": 500, "count": 1, "after_commit": False},
        )
        partial = _sync(client, revision4, "http-calendar-r4", via_run=False)
        assert partial["status"] == "PARTIALLY_SUCCEEDED", partial
        succeeded_ids = {
            item["id"] for item in partial["items"] if item["status"] == "SUCCEEDED"
        }
        recovered = _request(
            client,
            "POST",
            f"/calendar-operation-drafts/{partial['id']}/retry",
            expected=200,
        ).json()
        assert recovered["status"] == "SUCCEEDED"
        assert all(
            item["attempt_count"] == 1
            for item in recovered["items"]
            if item["id"] in succeeded_ids
        )
        assert stub.get("/admin/events").json()["event_count"] == (
            count_after_lost_response
        )

        negative_starts = [
            datetime.fromisoformat(item["scheduled_start"]) + timedelta(minutes=30)
            for item in revision4["sessions"]
        ]
        pending = _request(
            client,
            "POST",
            "/schedule-drafts",
            expected=201,
            json=_schedule_request(revision4, "http-negative-pending", negative_starts),
        ).json()
        pending_apply = {
            "client_request_id": "http-negative-pending-apply",
            "expected_draft_version": pending["version"],
            "root_plan_id": revision4.get("root_plan_id") or revision4["id"],
            "source_revision": revision4["revision"],
            "expected_plan_version": revision4["version"],
        }
        _request(
            client,
            "POST",
            f"/schedule-drafts/{pending['id']}/apply",
            expected=409,
            json=pending_apply,
        )
        accepted_pending = _request(
            client,
            "POST",
            f"/schedule-drafts/{pending['id']}/accept",
            expected=200,
            json={"expected_version": pending["version"]},
        ).json()
        pending_apply["expected_draft_version"] = accepted_pending["version"]
        validated = _request(
            client,
            "POST",
            f"/schedule-drafts/{pending['id']}/apply",
            expected=201,
            json=pending_apply,
        ).json()["plan"]
        _request(
            client,
            "POST",
            f"/plans/{root}/revisions/{validated['revision']}/ics-export",
            expected=409,
            json={
                "client_request_id": "http-negative-unconfirmed-ics",
                "expected_plan_version": validated["version"],
            },
        )
        unapproved = _request(
            client,
            "POST",
            f"/plans/{root}/revisions/4/calendar-operation-drafts",
            expected=201,
            json={
                "client_request_id": "http-negative-unapproved",
                "expected_plan_version": revision4["version"],
                "provider": "http",
                "calendar_id": "primary",
            },
        ).json()
        _request(
            client,
            "POST",
            f"/calendar-operation-drafts/{unapproved['id']}/execute",
            expected=409,
        )

        summary = {
            "live": live,
            "schedule_revision": revision2["revision"],
            "schedule_draft_status": draft2["status"],
            "ics_event_count": export1["event_count"],
            "ics_sha256": export1["content_sha256"],
            "first_sync": first_sync["status"],
            "reconcile_operations": sorted(operations),
            "partial_before_retry": partial["status"],
            "partial_after_retry": recovered["status"],
            "external_event_count": initial_event_count,
            "response_loss_recovery": recovered_loss["status"],
            "response_loss_duplicate_events": 0,
            "negative_pending_apply": 409,
            "negative_unconfirmed_ics": 409,
            "negative_unapproved_calendar": 409,
        }
        print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
