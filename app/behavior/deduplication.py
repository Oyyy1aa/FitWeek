"""Revision-lineage and canonical check-in deduplication."""

from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from app.domain.checkins.models import WorkoutCheckIn
from app.domain.plans.models import WeeklyPlan
from app.domain.sessions.models import WorkoutSession


@dataclass(frozen=True, slots=True)
class LogicalSessionFact:
    root_plan_id: UUID
    plan_revision: int
    session: WorkoutSession


@dataclass(frozen=True, slots=True)
class DeduplicatedBehaviorFacts:
    sessions: tuple[LogicalSessionFact, ...]
    check_ins: tuple[WorkoutCheckIn, ...]
    conflict_checkin_ids: tuple[UUID, ...]
    deduplicated_count: int


def deduplicate(
    plans: tuple[WeeklyPlan, ...], check_ins: tuple[WorkoutCheckIn, ...]
) -> DeduplicatedBehaviorFacts:
    latest_sessions: dict[tuple[UUID, UUID], LogicalSessionFact] = {}
    copied_count = 0
    for plan in sorted(plans, key=lambda item: (item.revision, str(item.id))):
        for session in plan.sessions:
            key = (plan.series_id, session.id)
            if key in latest_sessions:
                copied_count += 1
            latest_sessions[key] = LogicalSessionFact(
                root_plan_id=plan.series_id,
                plan_revision=plan.revision,
                session=session,
            )

    grouped: dict[tuple[UUID, UUID], list[WorkoutCheckIn]] = defaultdict(list)
    for item in check_ins:
        grouped[(item.plan_id, item.session_id)].append(item)
    canonical: list[WorkoutCheckIn] = []
    conflicts: list[UUID] = []
    duplicate_count = copied_count
    for session_key in sorted(grouped, key=lambda item: (str(item[0]), str(item[1]))):
        values = sorted(
            grouped[session_key],
            key=lambda item: (item.created_at, item.occurred_at, str(item.id)),
        )
        canonical.append(values[0])
        if len(values) > 1:
            duplicate_count += len(values) - 1
            conflicts.extend(item.id for item in values)
    sessions = tuple(
        item
        for item in sorted(
            latest_sessions.values(),
            key=lambda value: (
                value.session.scheduled_start,
                str(value.root_plan_id),
                str(value.session.id),
            ),
        )
    )
    return DeduplicatedBehaviorFacts(
        sessions=sessions,
        check_ins=tuple(canonical),
        conflict_checkin_ids=tuple(sorted(set(conflicts), key=str)),
        deduplicated_count=duplicate_count,
    )
