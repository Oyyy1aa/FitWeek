"""Phase 9A.1 Recovery clock portability — full-stack API tests.

These tests run on Python 3.12+ (require `from datetime import UTC`).
The architecture has been validated in `test_recovery_clock_portability.py`
which runs on Python 3.10. These full-stack tests use actual HTTP endpoints.

Run the phase_9a1 recovery-clock API test module with pytest.
"""

from datetime import UTC, datetime

import pytest

from tests.api.helpers import generation_payload, profile_payload

pytestmark = pytest.mark.phase_9a1

FIXED_UTC = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _recovery_payload(
    plan: dict,
    *,
    request_id: str = "clock-api-1",
    target_index: int = 0,
    message: str = "clock-portability test",
) -> dict:
    return {
        "client_request_id": request_id,
        "root_plan_id": plan["root_plan_id"] or plan["id"],
        "source_revision": plan["revision"],
        "expected_plan_version": plan["version"],
        "request_type": "RESCHEDULE_REQUEST",
        "target_session_ids": [plan["sessions"][target_index]["id"]],
        "user_request": message,
    }


class TestRecovery_ClockPortability_APITests:
    """Full-stack verification that Recovery immutability uses injected clock."""

    def test_sessions_before_fixed_now_are_immutable(self, api_client, monkeypatch):
        """All sessions in a June week are before 2026-07-01 → immutable."""
        monkeypatch.setattr("app.domain.common.utc_now", lambda: FIXED_UTC)
        monkeypatch.setattr(
            "app.application.recovery_applications.utc_now", lambda: FIXED_UTC
        )
        monkeypatch.setattr(
            "app.orchestration.clock.SystemClock.now",
            lambda _self: FIXED_UTC,
        )

        assert (
            api_client.put("/api/v1/profiles/me", json=profile_payload()).status_code
            == 200
        )

        slots = [
            {
                "start": (
                    datetime(2026, 6, 15 + i * 2, 10, 0, 0, tzinfo=UTC)
                ).isoformat(),
                "end": (
                    datetime(2026, 6, 15 + i * 2, 11, 0, 0, tzinfo=UTC)
                ).isoformat(),
                "location_type": "HOME",
            }
            for i in range(2)
        ]
        created = api_client.post(
            "/api/v1/plans/generate",
            json={
                "week_start": "2026-06-15",
                "availability_slots": slots,
                "preferred_locations": ["HOME"],
                "preferred_session_types": [],
            },
        )
        assert created.status_code == 201, created.text
        plan = created.json()["plan"]
        confirmed = api_client.post(
            f"/api/v1/plans/{plan['id']}/confirm",
            json={"expected_version": plan["version"]},
        )
        assert confirmed.status_code == 200, confirmed.text

        for idx in range(len(confirmed.json()["sessions"])):
            blocked = api_client.post(
                "/api/v1/recovery-drafts",
                json=_recovery_payload(
                    confirmed.json(), request_id=f"immut-api-{idx}", target_index=idx
                ),
            )
            assert blocked.status_code == 422, blocked.text
            assert (
                blocked.json()["error"]["code"] == "RECOVERY_TARGET_SESSION_IMMUTABLE"
            )

    def test_sessions_after_fixed_now_are_mutable(self, api_client, monkeypatch):
        """July sessions are after 2026-07-01 → mutable."""
        monkeypatch.setattr("app.domain.common.utc_now", lambda: FIXED_UTC)
        monkeypatch.setattr(
            "app.application.recovery_applications.utc_now", lambda: FIXED_UTC
        )
        monkeypatch.setattr(
            "app.orchestration.clock.SystemClock.now",
            lambda _self: FIXED_UTC,
        )

        assert (
            api_client.put("/api/v1/profiles/me", json=profile_payload()).status_code
            == 200
        )

        created = api_client.post("/api/v1/plans/generate", json=generation_payload())
        assert created.status_code == 201, created.text
        plan = created.json()["plan"]
        confirmed = api_client.post(
            f"/api/v1/plans/{plan['id']}/confirm",
            json={"expected_version": plan["version"]},
        )
        assert confirmed.status_code == 200, confirmed.text

        draft = api_client.post(
            "/api/v1/recovery-drafts",
            json=_recovery_payload(
                confirmed.json(), request_id="mutable-api", target_index=1
            ),
        )
        assert draft.status_code == 201, draft.text
        assert draft.json()["outcome"] in {
            "COMPLETE",
            "PARTIAL",
            "NO_CHANGE",
            "USER_ACTION_REQUIRED",
        }

    def test_default_checkin_immutability_unchanged(self, api_client):
        """Without monkeypatching, the existing check-in immutability still works."""
        assert (
            api_client.put("/api/v1/profiles/me", json=profile_payload()).status_code
            == 200
        )

        created = api_client.post("/api/v1/plans/generate", json=generation_payload())
        assert created.status_code == 201, created.text
        plan = created.json()["plan"]
        confirmed = api_client.post(
            f"/api/v1/plans/{plan['id']}/confirm",
            json={"expected_version": plan["version"]},
        )
        assert confirmed.status_code == 200, confirmed.text

        session = confirmed.json()["sessions"][0]
        checkin = api_client.post(
            f"/api/v1/sessions/{session['id']}/check-ins",
            json={
                "client_event_id": "portability-api-checkin",
                "status": "COMPLETED",
                "actual_minutes": 30,
                "perceived_effort": 5,
                "note": "portability API test check-in",
                "occurred_at": datetime(2026, 7, 20, 11, tzinfo=UTC).isoformat(),
            },
        )
        assert checkin.status_code == 201, checkin.text

        blocked = api_client.post(
            "/api/v1/recovery-drafts",
            json=_recovery_payload(
                confirmed.json(), request_id="default-api-immut", target_index=0
            ),
        )
        assert blocked.status_code == 422
        assert blocked.json()["error"]["code"] == "RECOVERY_TARGET_SESSION_IMMUTABLE"
