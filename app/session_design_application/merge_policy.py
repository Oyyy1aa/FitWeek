"""Pure one-Session composition merge over the shared Plan Revision model."""

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from app.domain.checkins.models import WorkoutCheckIn
from app.domain.plans.models import WeeklyPlan
from app.domain.session_design.models import SessionDesignDraft
from app.domain.session_design_application.models import (
    SessionCompositionSummary,
    SessionDesignApplicationMetadata,
)
from app.domain.sessions.models import WorkoutSession, WorkoutSessionStatus
from app.replanning.revision_builder import PlanRevisionBuilder
from app.session_design.duration import SessionDurationPolicy


class SessionDesignPlanMergePolicy:
    """Replace only the accepted exercise composition in a new Plan snapshot."""

    version = "session-design-plan-apply-v1"

    def __init__(self, builder: PlanRevisionBuilder | None = None) -> None:
        self._builder = builder or PlanRevisionBuilder()
        self._duration = SessionDurationPolicy()

    def build(
        self,
        *,
        source: WeeklyPlan,
        draft: SessionDesignDraft,
        target: WorkoutSession,
        check_ins: tuple[WorkoutCheckIn, ...],
        fingerprint: str,
        created_at: datetime,
    ) -> tuple[WeeklyPlan, SessionDesignApplicationMetadata]:
        check_in_ids = {item.session_id for item in check_ins}
        immutable: list[UUID] = []
        preserved: list[UUID] = []
        sessions: list[WorkoutSession] = []
        terminal = {
            WorkoutSessionStatus.COMPLETED,
            WorkoutSessionStatus.SKIPPED,
            WorkoutSessionStatus.CANCELLED,
        }
        for session in source.sessions:
            if session.id == target.id:
                sessions.append(
                    replace(
                        session,
                        exercises=draft.exercises,
                        version=session.version + 1,
                    )
                )
                continue
            sessions.append(session)
            if (
                session.id in check_in_ids
                or session.status in terminal
                or session.scheduled_start <= created_at
            ):
                immutable.append(session.id)
            else:
                preserved.append(session.id)
        metadata = SessionDesignApplicationMetadata(
            source_draft_id=draft.id,
            source_draft_version=draft.version,
            source_candidate_set_id=draft.candidate_set_id,
            source_context_snapshot_reference_id=draft.context_snapshot_reference_id,
            source_revision=source.revision,
            target_session_id=target.id,
            changed_session_ids=(target.id,),
            preserved_session_ids=tuple(preserved),
            immutable_session_ids=tuple(immutable),
            application_fingerprint=fingerprint,
        )
        return (
            self._builder.build_session_design_application(
                source=source,
                sessions=tuple(sessions),
                metadata=metadata,
                created_at=created_at,
            ),
            metadata,
        )

    def summarize(self, session: WorkoutSession) -> SessionCompositionSummary:
        duration = self._duration.calculate(session.exercises)
        return SessionCompositionSummary(
            session_id=session.id,
            session_type=session.session_type,
            session_version=session.version,
            exercise_ids=tuple(item.exercise_id for item in session.exercises),
            exercises=session.exercises,
            calculated_seconds=duration.total_seconds,
        )
