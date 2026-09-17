"""Build deterministic summaries from plan/check-in snapshots."""

import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.behavior.deduplication import deduplicate
from app.behavior.evidence_policy import build_evidence, time_bucket
from app.behavior.metrics import RecoveryMetrics
from app.behavior.signal_policy import build_patterns
from app.domain.behavior.models import BehaviorSummary, BehaviorSummaryWindow
from app.domain.behavior.policies import BEHAVIOR_SUMMARY_POLICY_VERSION
from app.domain.checkins.models import CheckInStatus, WorkoutCheckIn
from app.domain.plans.models import WeeklyPlan
from app.orchestration.clock import Clock


class BehaviorSummaryBuilder:
    """Pure computation except for process-local metric counters."""

    def __init__(
        self,
        *,
        clock: Clock,
        metrics: RecoveryMetrics,
        default_window_days: int = 28,
        max_window_days: int = 56,
        min_signal_occurrences: int = 3,
        repeat_ratio_threshold: Decimal = Decimal("0.60"),
        min_rpe_samples: int = 2,
    ) -> None:
        self._clock = clock
        self._metrics = metrics
        self._default_days = default_window_days
        self._max_days = max_window_days
        self._min_occurrences = min_signal_occurrences
        self._ratio_threshold = repeat_ratio_threshold
        self._min_rpe_samples = min_rpe_samples

    def build(
        self,
        *,
        user_id: UUID,
        timezone: str,
        plans: tuple[WeeklyPlan, ...],
        check_ins: tuple[WorkoutCheckIn, ...],
        window: BehaviorSummaryWindow | None = None,
    ) -> BehaviorSummary:
        now = self._clock.now()
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        start_utc, end_utc = self._window_bounds(now, zone, window)
        user_plans = tuple(item for item in plans if item.user_id == user_id)
        user_checkins = tuple(
            item
            for item in check_ins
            if item.user_id == user_id and item.occurred_at < min(end_utc, now)
        )
        facts = deduplicate(user_plans, user_checkins)
        session_facts = tuple(
            item
            for item in facts.sessions
            if start_utc <= item.session.scheduled_start < min(end_utc, now)
        )
        sessions_by_key = {
            (item.root_plan_id, item.session.id): item for item in session_facts
        }
        canonical = tuple(
            item
            for item in facts.check_ins
            if (item.plan_id, item.session_id) in sessions_by_key
        )
        evidence = tuple(
            sorted(
                (
                    build_evidence(
                        check_in=item,
                        session=sessions_by_key[
                            (item.plan_id, item.session_id)
                        ].session,
                        root_plan_id=sessions_by_key[
                            (item.plan_id, item.session_id)
                        ].root_plan_id,
                        timezone=zone,
                    )
                    for item in canonical
                ),
                key=lambda item: (item.scheduled_at_utc, str(item.checkin_id)),
            )
        )
        completed = sum(item.status is CheckInStatus.COMPLETED for item in evidence)
        partial = sum(
            item.status is CheckInStatus.PARTIALLY_COMPLETED for item in evidence
        )
        skipped = sum(item.status is CheckInStatus.SKIPPED for item in evidence)
        scheduled = len(session_facts)
        checked = len(evidence)
        rpes = [item.reported_rpe for item in evidence if item.reported_rpe is not None]
        patterns = build_patterns(
            evidence,
            min_occurrences=self._min_occurrences,
            ratio_threshold=self._ratio_threshold,
            scheduled_dimensions=tuple(
                (
                    item.scheduled_start.astimezone(zone).strftime("%A").upper(),
                    time_bucket(item.scheduled_start.astimezone(zone)),
                    item.location_type.value,
                )
                for fact in session_facts
                for item in (fact.session,)
            ),
        )
        time_patterns = tuple(
            item
            for item in patterns
            if "TIME_OF_DAY" in item.pattern_type.value
            or item.pattern_type.value
            in {
                "REPEATED_PARTIAL_COMPLETION",
                "REPEATED_HIGH_REPORTED_RPE",
            }
        )
        location_patterns = tuple(
            item for item in patterns if "LOCATION" in item.pattern_type.value
        )
        skip_patterns = tuple(
            item for item in patterns if "SKIP" in item.pattern_type.value
        )
        payload = {
            "user_id": str(user_id),
            "window_start": start_utc.isoformat(),
            "window_end": end_utc.isoformat(),
            "timezone": timezone,
            "sessions": [
                f"{item.root_plan_id}:{item.session.id}" for item in session_facts
            ],
            "evidence": [item.fingerprint for item in evidence],
            "conflicts": [str(item) for item in facts.conflict_checkin_ids],
            "patterns": [item.pattern_id for item in patterns],
            "policy": BEHAVIOR_SUMMARY_POLICY_VERSION,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        quant = Decimal("0.0001")
        summary = BehaviorSummary(
            id=uuid5(NAMESPACE_URL, f"behavior-summary:{fingerprint}"),
            user_id=user_id,
            window_start_utc=start_utc,
            window_end_utc=end_utc,
            timezone=timezone,
            scheduled_session_count=scheduled,
            checked_in_session_count=checked,
            completed_count=completed,
            partially_completed_count=partial,
            skipped_count=skipped,
            missing_checkin_count=scheduled - checked,
            completion_rate=(
                (Decimal(completed) / Decimal(scheduled)).quantize(
                    quant, rounding=ROUND_HALF_UP
                )
                if scheduled
                else None
            ),
            participation_rate=(
                (Decimal(completed + partial) / Decimal(scheduled)).quantize(
                    quant, rounding=ROUND_HALF_UP
                )
                if scheduled
                else None
            ),
            rpe_sample_count=len(rpes),
            average_reported_rpe=(
                (Decimal(sum(rpes)) / Decimal(len(rpes))).quantize(
                    quant, rounding=ROUND_HALF_UP
                )
                if len(rpes) >= self._min_rpe_samples
                else None
            ),
            high_reported_rpe_count=sum(value >= 8 for value in rpes),
            repeated_time_patterns=time_patterns,
            repeated_location_patterns=location_patterns,
            repeated_skip_patterns=skip_patterns,
            evidence_references=evidence,
            conflict_checkin_ids=facts.conflict_checkin_ids,
            policy_version=BEHAVIOR_SUMMARY_POLICY_VERSION,
            fingerprint=fingerprint,
            created_at=now,
        )
        self._metrics.behavior_summaries_built += 1
        self._metrics.behavior_checkins_deduplicated += facts.deduplicated_count
        self._metrics.behavior_conflicts_detected += int(
            bool(facts.conflict_checkin_ids)
        )
        self._metrics.behavior_patterns_created += len(patterns)
        return summary

    def _window_bounds(
        self,
        now: datetime,
        zone: ZoneInfo,
        requested: BehaviorSummaryWindow | None,
    ) -> tuple[datetime, datetime]:
        today = now.astimezone(zone).date()
        end_date = (
            requested.end_date
            if requested and requested.end_date
            else today + timedelta(days=1)
        )
        start_date = (
            requested.start_date
            if requested and requested.start_date
            else end_date - timedelta(days=self._default_days)
        )
        days = (end_date - start_date).days
        if days < 1 or days > self._max_days:
            raise ValueError(
                f"behavior window must be between 1 and {self._max_days} days"
            )
        end_date = min(end_date, today + timedelta(days=1))
        start_local = datetime.combine(start_date, time.min, tzinfo=zone)
        end_local = datetime.combine(end_date, time.min, tzinfo=zone)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)
