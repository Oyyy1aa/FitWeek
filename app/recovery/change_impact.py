"""Read-only classification of current confirmed Plan Sessions."""

import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.calendar_operations.models import CalendarEventBinding
from app.domain.checkins.models import CheckInStatus, WorkoutCheckIn
from app.domain.plans.models import WeeklyPlan
from app.domain.recovery.enums import RecoveryRequestType
from app.domain.recovery.models import RecoveryChangeImpactSnapshot
from app.domain.sessions.models import WorkoutSessionStatus
from app.orchestration.clock import Clock


class RecoveryChangeImpactAnalyzer:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def analyze(
        self,
        *,
        user_id: UUID,
        plan: WeeklyPlan,
        check_ins: tuple[WorkoutCheckIn, ...],
        calendar_bindings: tuple[CalendarEventBinding, ...],
        request_type: RecoveryRequestType,
        target_session_ids: tuple[UUID, ...] | None,
    ) -> RecoveryChangeImpactSnapshot:
        now = self._clock.now()
        check_by_session = {item.session_id: item for item in check_ins}
        mutable: list[UUID] = []
        immutable: list[UUID] = []
        for session in sorted(plan.sessions, key=lambda item: str(item.id)):
            is_mutable = (
                session.status is WorkoutSessionStatus.PLANNED
                and session.scheduled_start > now
                and session.id not in check_by_session
            )
            (mutable if is_mutable else immutable).append(session.id)
        requested = set(target_session_ids or mutable)
        preserved = sorted(set(mutable) - requested, key=str)
        bound = sorted(
            {
                item.session_id
                for item in calendar_bindings
                if item.root_plan_id == plan.series_id
            },
            key=str,
        )
        payload = {
            "user": str(user_id),
            "root": str(plan.series_id),
            "revision": plan.revision,
            "version": plan.version,
            "mutable": [str(item) for item in mutable],
            "immutable": [str(item) for item in immutable],
            "preserved": [str(item) for item in preserved],
            "bindings": [str(item) for item in bound],
            "checkins": [str(item.id) for item in check_ins],
            "request_type": request_type.value,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        change_requested = request_type not in {
            RecoveryRequestType.GENERAL_RECOVERY_REVIEW,
            RecoveryRequestType.NEXT_WEEK_REVIEW,
        }
        requires_redesign = request_type in {
            RecoveryRequestType.REDUCE_FUTURE_LOAD,
            RecoveryRequestType.REPLACE_FUTURE_SESSION,
        }
        requires_schedule = request_type is RecoveryRequestType.RESCHEDULE_REQUEST
        return RecoveryChangeImpactSnapshot(
            id=uuid5(NAMESPACE_URL, f"recovery-impact:{fingerprint}"),
            user_id=user_id,
            root_plan_id=plan.series_id,
            source_revision=plan.revision,
            source_plan_version=plan.version,
            mutable_session_ids=tuple(sorted(mutable, key=str)),
            immutable_session_ids=tuple(sorted(immutable, key=str)),
            preserved_session_ids=tuple(preserved),
            calendar_bound_session_ids=tuple(bound),
            completed_checkin_ids=tuple(
                sorted(
                    (
                        item.id
                        for item in check_ins
                        if item.status is CheckInStatus.COMPLETED
                    ),
                    key=str,
                )
            ),
            weekly_frequency_before=len(plan.sessions),
            minimum_allowed_frequency=2,
            maximum_allowed_frequency=5,
            requires_session_redesign=requires_redesign,
            requires_schedule_redraft=requires_schedule,
            requires_calendar_reconciliation=(
                change_requested and bool(set(bound) & requested)
            ),
            requires_new_plan_revision=change_requested,
            fingerprint=fingerprint,
            created_at=now,
        )
