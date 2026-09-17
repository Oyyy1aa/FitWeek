"""Frozen deterministic Time Slot Candidate Set construction."""

import hashlib
import json
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from app.domain.common import DomainValidationError
from app.domain.context.models import ContextSnapshotReference
from app.domain.scheduling.models import (
    AvailabilityWindow,
    BusySnapshot,
    SessionSlotCandidates,
    TimeSlotCandidate,
    TimeSlotCandidateSet,
)
from app.domain.sessions.models import WorkoutSession
from app.scheduling.time_policy import overlaps


class TimeSlotCandidateSetBuilder:
    policy_version = "schedule-candidate-set-v1"

    def __init__(
        self,
        *,
        granularity_minutes: int = 15,
        buffer_minutes: int = 0,
        max_per_session: int = 64,
        max_total: int = 512,
    ) -> None:
        if granularity_minutes <= 0 or buffer_minutes < 0:
            raise ValueError("invalid candidate timing configuration")
        self._step = timedelta(minutes=granularity_minutes)
        self._buffer = timedelta(minutes=buffer_minutes)
        self._max_per_session = max_per_session
        self._max_total = max_total

    def build(
        self,
        *,
        user_id: UUID,
        root_plan_id: UUID,
        source_revision: int,
        sessions: tuple[WorkoutSession, ...],
        availability: tuple[AvailabilityWindow, ...],
        busy: BusySnapshot,
        immutable_sessions: tuple[WorkoutSession, ...],
        context: ContextSnapshotReference,
        created_at: datetime,
        preferred_times: tuple[str, ...] = (),
        preferred_locations: tuple[str, ...] = (),
    ) -> TimeSlotCandidateSet:
        slots: list[TimeSlotCandidate] = []
        grouped: list[SessionSlotCandidates] = []
        for session in sorted(
            sessions, key=lambda item: (item.scheduled_start, item.id)
        ):
            duration = timedelta(minutes=session.estimated_minutes)
            session_slots: list[TimeSlotCandidate] = []
            for window in sorted(
                availability, key=lambda item: (item.start, item.end, item.location)
            ):
                if window.location is not session.location_type:
                    continue
                start = window.start
                while start + duration <= window.end:
                    end = start + duration
                    blocked = any(
                        overlaps(
                            start - self._buffer,
                            end + self._buffer,
                            item.start,
                            item.end,
                        )
                        for item in busy.intervals
                    ) or any(
                        item.id != session.id
                        and overlaps(
                            start, end, item.scheduled_start, item.scheduled_end
                        )
                        for item in immutable_sessions
                    )
                    if not blocked:
                        raw = (
                            f"{session.id}:{start.isoformat()}:{end.isoformat()}:"
                            f"{window.location.value}:{self.policy_version}"
                        )
                        slot_id = (
                            "slot_" + hashlib.sha256(raw.encode()).hexdigest()[:20]
                        )
                        local_hour = start.astimezone(ZoneInfo(busy.timezone)).hour
                        period = (
                            "morning"
                            if local_hour < 12
                            else "afternoon"
                            if local_hour < 18
                            else "evening"
                        )
                        preference_score = (
                            (
                                4
                                if start.astimezone(ZoneInfo(busy.timezone)).date()
                                == session.scheduled_start.astimezone(
                                    ZoneInfo(busy.timezone)
                                ).date()
                                else 0
                            )
                            + (
                                2
                                if window.location.value.casefold()
                                in preferred_locations
                                else 0
                            )
                            + (1 if period in preferred_times else 0)
                        )
                        session_slots.append(
                            TimeSlotCandidate(
                                session_id=session.id,
                                slot_id=slot_id,
                                start=start,
                                end=end,
                                location=window.location,
                                preference_score=preference_score,
                                id=uuid5(NAMESPACE_URL, raw),
                                timezone=busy.timezone,
                                source_availability_id=(
                                    window.id
                                    if window.id is not None
                                    else uuid5(NAMESPACE_URL, raw + ":availability")
                                ),
                            )
                        )
                    start += self._step
            session_slots.sort(
                key=lambda item: (-item.preference_score, item.start, item.slot_id)
            )
            session_slots = session_slots[: self._max_per_session]
            remaining = max(self._max_total - len(slots), 0)
            session_slots = session_slots[:remaining]
            if not session_slots:
                raise DomainValidationError(
                    "No legal time slot candidates are available.",
                    code="NO_VALID_TIME_SLOT",
                )
            slots.extend(session_slots)
            grouped.append(
                SessionSlotCandidates(
                    session_id=session.id,
                    slot_ids=tuple(item.slot_id for item in session_slots),
                )
            )
        payload = {
            "root_plan_id": str(root_plan_id),
            "source_revision": source_revision,
            "busy_fingerprint": busy.fingerprint,
            "context_snapshot_reference_id": str(context.id),
            "context_fingerprint": context.context_fingerprint,
            "slots": [
                (
                    item.slot_id,
                    str(item.session_id),
                    item.start.isoformat(),
                    item.end.isoformat(),
                    item.location.value,
                    item.preference_score,
                    item.timezone,
                    str(item.source_availability_id),
                )
                for item in slots
            ],
            "policy": self.policy_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return TimeSlotCandidateSet(
            id=uuid5(
                NAMESPACE_URL, f"fitweek:schedule-candidates:{user_id}:{fingerprint}"
            ),
            user_id=user_id,
            root_plan_id=root_plan_id,
            source_revision=source_revision,
            busy_snapshot_id=busy.id,
            context_snapshot_reference_id=context.id,
            slots=tuple(slots),
            session_candidates=tuple(grouped),
            fingerprint=fingerprint,
            policy_version=self.policy_version,
            created_at=created_at,
        )
