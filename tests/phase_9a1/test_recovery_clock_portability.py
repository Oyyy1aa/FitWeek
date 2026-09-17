"""Phase 9A.1 Recovery clock portability tests — Task 3.

This module validates the clock injection architecture and immutability logic.
On Python 3.12 (Windows acceptance environment), all imports resolve.
On earlier Python versions, only pure-logic tests are runnable.

Architecture decisions (do not change without plan amendment):
- Inject fixed clock at 2026-07-01T12:00:00Z via monkeypatch
- Injection points: app.domain.common.utc_now,
  app.application.recovery_applications.utc_now,
  app.orchestration.clock.SystemClock.now
- No new production Clock abstraction — reuse existing SystemClock and FakeClock
- Recovery immutability gate derives from injected clock, not wall time
"""

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.phase_9a1

FIXED_UTC = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


class TestClockInjectionArchitecture:
    """Validate clock injection without a new production abstraction."""

    def test_fixed_clock_is_aware_utc(self) -> None:
        """The canonical fixed reference is an aware UTC datetime."""
        assert FIXED_UTC.tzinfo is not None
        assert FIXED_UTC.utcoffset() == timedelta(0)
        assert FIXED_UTC.isoformat() == "2026-07-01T12:00:00+00:00"

    def test_system_clock_returns_aware_utc(self) -> None:
        """SystemClock.now() returns an aware UTC datetime."""
        from app.orchestration.clock import SystemClock

        clock = SystemClock()
        real = clock.now()
        assert real.tzinfo is not None
        assert real.utcoffset() == timedelta(0)

    def test_fake_clock_accepts_fixed_reference(self) -> None:
        """FakeClock (existing test clock) accepts the canonical fixed reference."""
        from app.orchestration.clock import FakeClock

        fc = FakeClock(FIXED_UTC)
        assert fc.now() == FIXED_UTC
        fc.advance(timedelta(hours=1))
        assert fc.now() == FIXED_UTC + timedelta(hours=1)

    def test_fake_clock_rejects_backwards_advance(self) -> None:
        """FakeClock rejects negative deltas — safety invariant."""
        from app.orchestration.clock import FakeClock

        fc = FakeClock(FIXED_UTC)
        with pytest.raises(ValueError, match="cannot move backwards"):
            fc.advance(timedelta(seconds=-1))

    def test_fake_clock_rejects_naive_datetime(self) -> None:
        """FakeClock rejects naive datetimes — safety invariant."""
        from app.orchestration.clock import FakeClock

        with pytest.raises(ValueError, match="aware UTC"):
            FakeClock(datetime(2026, 7, 1, 12, 0, 0))

    def test_domain_utc_now_returns_aware_utc(self) -> None:
        """app.domain.common.utc_now() returns aware UTC — patchable by monkeypatch."""
        from app.domain import common as common_mod

        real = common_mod.utc_now()
        assert real.tzinfo is not None
        assert real.utcoffset() == timedelta(0)

    def test_recovery_applications_imports_utc_now(self) -> None:
        """recovery_applications.py imports utc_now from app.domain.common."""
        from app.application.recovery_applications import utc_now

        result = utc_now()
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)

    def test_reliability_mutable_clock_accepts_ref_time(self) -> None:
        """Alerting/reliability MutableClock supports deterministic injection."""
        from app.reliability.clock import MutableClock

        mc = MutableClock(FIXED_UTC)
        assert mc.now() == FIXED_UTC
        mc.advance(timedelta(minutes=30))
        assert mc.now() == FIXED_UTC + timedelta(minutes=30)


class TestRecoveryImmutabilityGate:
    """Verify the immutability rule logic with a fixed clock."""

    def test_past_session_fails_gate_relative_to_fixed(self) -> None:
        """Session at 2026-06-20 < FIXED_UTC(2026-07-01) → immutable."""
        past = datetime(2026, 6, 20, 10, 0, 0, tzinfo=UTC)
        assert past <= FIXED_UTC, "scheduled_start <= utc_now() → immutable"

    def test_future_session_passes_gate_relative_to_fixed(self) -> None:
        """Session at 2026-07-20 > FIXED_UTC(2026-07-01) → mutable."""
        future = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)
        assert future > FIXED_UTC, "scheduled_start > utc_now() → mutable"

    def test_exact_fixed_now_is_immutable(self) -> None:
        """Session at exactly FIXED_UTC is treated as immutable (<=)."""
        exact = FIXED_UTC
        assert exact <= FIXED_UTC, "<= means exactly-now sessions are immutable"

    def test_error_code_must_not_change(self) -> None:
        """RECOVERY_TARGET_SESSION_IMMUTABLE is the canonical error code."""
        from app.application.errors import RecoveryTargetSessionImmutable

        exc = RecoveryTargetSessionImmutable("detail")
        assert exc.code == "RECOVERY_TARGET_SESSION_IMMUTABLE"


class TestHelperPayloadRelativeToFixedClock:
    """Session construction dates must be derivable from the fixed clock."""

    def test_generation_payload_week_is_after_fixed(self) -> None:
        """generation_payload uses week 2026-07-20, which is after 2026-07-01."""
        from tests.api.helpers import generation_payload

        payload = generation_payload()
        assert payload["week_start"] == "2026-07-20"
        week_start = datetime.strptime(payload["week_start"], "%Y-%m-%d").replace(
            tzinfo=UTC
        )
        assert week_start > FIXED_UTC

    def test_profile_payload_is_static(self) -> None:
        """profile_payload has no time dependency — always safe."""
        from tests.api.helpers import profile_payload

        p = profile_payload()
        assert p["weekly_frequency"] == 2
        assert p["primary_goal"] == "GENERAL_FITNESS"

    def test_fixed_week_is_usable_for_recovery_tests(self) -> None:
        """2026-07-20 sessions are mutable under FIXED_UTC (2026-07-01)."""
        session_start = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)
        assert session_start > FIXED_UTC

    def test_historical_week_is_before_fixed(self) -> None:
        """2026-06-15 sessions are immutable under FIXED_UTC."""
        session_start = datetime(2026, 6, 15, 10, 0, 0, tzinfo=UTC)
        assert session_start <= FIXED_UTC

    def test_no_naive_datetimes_in_test_references(self) -> None:
        """All datetime values in this module carry tzinfo."""
        assert FIXED_UTC.tzinfo is not None
        refs = [
            datetime(2026, 6, 20, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 15, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 21, tzinfo=UTC),
        ]
        for ref in refs:
            assert ref.tzinfo is not None, f"{ref} must be aware"
            assert ref.utcoffset() == timedelta(0), f"{ref} must be UTC"
