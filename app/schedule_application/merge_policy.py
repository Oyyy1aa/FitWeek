"""Pure time-only merge from a frozen Schedule Draft into a new Plan Revision."""

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from app.domain.checkins.models import WorkoutCheckIn
from app.domain.plans.models import WeeklyPlan
from app.domain.schedule_application.models import ScheduleApplicationMetadata
from app.domain.scheduling.models import ScheduleDraft
from app.domain.sessions.models import WorkoutSessionStatus
from app.replanning.revision_builder import PlanRevisionBuilder


class SchedulePlanMergePolicy:
    version = "schedule-plan-apply-v1"

    def __init__(self, builder: PlanRevisionBuilder | None = None) -> None:
        self._builder = builder or PlanRevisionBuilder()

    def build(
        self,
        *,
        source: WeeklyPlan,
        draft: ScheduleDraft,
        check_ins: tuple[WorkoutCheckIn, ...],
        fingerprint: str,
        created_at: datetime,
    ) -> tuple[WeeklyPlan, ScheduleApplicationMetadata]:
        assignments = {item.session_id: item for item in draft.assignments}
        checked = {item.session_id for item in check_ins}
        terminal = {
            WorkoutSessionStatus.COMPLETED,
            WorkoutSessionStatus.SKIPPED,
            WorkoutSessionStatus.CANCELLED,
        }
        changed: list[UUID] = []
        preserved: list[UUID] = []
        immutable: list[UUID] = []
        sessions = []
        for session in source.sessions:
            assignment = assignments.get(session.id)
            if assignment is not None:
                schedule_source_metadata = (
                    ("schedule_draft_id", str(draft.id)),
                    ("busy_snapshot_id", str(draft.busy_snapshot_id)),
                    ("candidate_set_id", str(draft.candidate_set_id)),
                    (
                        "context_snapshot_reference_id",
                        str(draft.context_snapshot_reference_id),
                    ),
                    ("timezone_policy_version", "timezone-policy-v1"),
                )
                sessions.append(
                    replace(
                        session,
                        scheduled_start=assignment.scheduled_start,
                        scheduled_end=assignment.scheduled_end,
                        version=session.version + 1,
                        schedule_source_metadata=schedule_source_metadata,
                    )
                )
                changed.append(session.id)
            else:
                sessions.append(session)
                if (
                    session.id in checked
                    or session.status in terminal
                    or session.scheduled_start <= created_at
                ):
                    immutable.append(session.id)
                else:
                    preserved.append(session.id)
        metadata = ScheduleApplicationMetadata(
            source_schedule_draft_id=draft.id,
            source_schedule_draft_version=draft.version,
            source_context_snapshot_reference_id=draft.context_snapshot_reference_id,
            source_busy_snapshot_id=draft.busy_snapshot_id,
            source_candidate_set_id=draft.candidate_set_id,
            source_revision=source.revision,
            created_revision=source.revision + 1,
            changed_session_ids=tuple(changed),
            preserved_session_ids=tuple(preserved),
            immutable_session_ids=tuple(immutable),
            calendar_verification_status=draft.calendar_verification_status,
            application_fingerprint=fingerprint,
        )
        revision = self._builder.build_schedule_application(
            source=source,
            sessions=tuple(sessions),
            metadata=metadata,
            created_at=created_at,
        )
        return revision, metadata
