"""All-or-nothing merge of accepted Recovery child drafts into one Revision."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from app.application.errors import (
    RecoveryFrequencyViolation,
    RecoverySubdraftRejected,
)
from app.domain.checkins.models import WorkoutCheckIn
from app.domain.plans.models import WeeklyPlan
from app.domain.recovery.models import RecoveryActionCandidateSet, RecoveryDraft
from app.domain.recovery_application.models import RecoveryPlanApplicationMetadata
from app.domain.scheduling.enums import ScheduleDraftOutcome, ScheduleDraftStatus
from app.domain.scheduling.models import ScheduleDraft
from app.domain.session_design.enums import SessionDesignDraftStatus
from app.domain.session_design.models import SessionDesignDraft
from app.domain.sessions.models import WorkoutSession, WorkoutSessionStatus
from app.recovery.spacing_validator import RecoverySpacingValidator
from app.recovery_application.resolution_policy import ResolvedRecoveryActions
from app.replanning.revision_builder import PlanRevisionBuilder


class RecoveryPlanMergePolicy:
    version = "recovery-plan-application-v1"

    def __init__(self, builder: PlanRevisionBuilder | None = None) -> None:
        self._builder = builder or PlanRevisionBuilder()
        self._spacing = RecoverySpacingValidator()

    def build(
        self,
        *,
        source: WeeklyPlan,
        recovery_draft: RecoveryDraft,
        candidate_set: RecoveryActionCandidateSet,
        resolved: ResolvedRecoveryActions,
        session_design_drafts: tuple[SessionDesignDraft, ...],
        session_design_targets: dict[UUID, UUID],
        schedule_drafts: tuple[ScheduleDraft, ...],
        check_ins: tuple[WorkoutCheckIn, ...],
        fingerprint: str,
        created_at: datetime,
    ) -> WeeklyPlan:
        designs = self._designs(session_design_drafts, session_design_targets, resolved)
        schedules = self._schedules(schedule_drafts, resolved)
        checked = {item.session_id for item in check_ins}
        immutable: list[UUID] = []
        preserved: list[UUID] = []
        changed: set[UUID] = set()
        sessions: list[WorkoutSession] = []
        terminal = {
            WorkoutSessionStatus.COMPLETED,
            WorkoutSessionStatus.SKIPPED,
            WorkoutSessionStatus.CANCELLED,
        }
        for session in source.sessions:
            is_immutable = (
                session.id in checked
                or session.status in terminal
                or session.scheduled_start <= created_at
            )
            if is_immutable:
                immutable.append(session.id)
            if session.id in resolved.remove_session_ids:
                if is_immutable:
                    raise RecoverySubdraftRejected(
                        "An immutable Session cannot be removed by Recovery."
                    )
                changed.add(session.id)
                continue
            updated = session
            design = designs.get(session.id)
            if design is not None:
                if is_immutable:
                    raise RecoverySubdraftRejected(
                        "An immutable Session cannot be redesigned by Recovery."
                    )
                updated = replace(
                    updated,
                    exercises=design.exercises,
                    version=updated.version + 1,
                )
                changed.add(session.id)
            assignment = schedules.get(session.id)
            if assignment is not None:
                if is_immutable:
                    raise RecoverySubdraftRejected(
                        "An immutable Session cannot be rescheduled by Recovery."
                    )
                updated = replace(
                    updated,
                    scheduled_start=assignment.scheduled_start,
                    scheduled_end=assignment.scheduled_end,
                    version=updated.version + 1,
                    schedule_source_metadata=(
                        ("recovery_schedule_draft_id", str(assignment.draft_id)),
                        ("recovery_policy_version", self.version),
                    ),
                )
                changed.add(session.id)
            sessions.append(updated)
            if session.id not in changed and not is_immutable:
                preserved.append(session.id)
        spacing = self._spacing.validate(
            plan=replace(
                source,
                sessions=tuple(sessions),
                estimated_total_minutes=sum(
                    item.estimated_minutes for item in sessions
                ),
            ),
            selected=(),
        )
        if not spacing.passed:
            raise RecoveryFrequencyViolation(
                "The Recovery revision violates frequency or spacing bounds."
            )
        metadata = RecoveryPlanApplicationMetadata(
            source_recovery_draft_id=recovery_draft.id,
            source_recovery_draft_version=recovery_draft.version,
            source_candidate_set_id=candidate_set.id,
            source_behavior_summary_id=recovery_draft.behavior_summary_id,
            source_change_impact_snapshot_id=recovery_draft.change_impact_snapshot_id,
            source_revision=source.revision,
            created_revision=source.revision + 1,
            applied_action_candidate_ids=tuple(
                sorted(recovery_draft.selected_action_candidate_ids, key=str)
            ),
            session_design_draft_ids=tuple(
                sorted((item.id for item in session_design_drafts), key=str)
            ),
            schedule_draft_ids=tuple(
                sorted((item.id for item in schedule_drafts), key=str)
            ),
            changed_session_ids=tuple(sorted(changed, key=str)),
            removed_session_ids=resolved.remove_session_ids,
            preserved_session_ids=tuple(sorted(preserved, key=str)),
            immutable_session_ids=tuple(sorted(immutable, key=str)),
            application_fingerprint=fingerprint,
        )
        return self._builder.build_recovery_application(
            source=source,
            sessions=tuple(sessions),
            metadata=metadata,
            created_at=created_at,
        )

    @staticmethod
    def _designs(
        drafts: tuple[SessionDesignDraft, ...],
        targets: dict[UUID, UUID],
        resolved: ResolvedRecoveryActions,
    ) -> dict[UUID, SessionDesignDraft]:
        if any(item.status is not SessionDesignDraftStatus.ACCEPTED for item in drafts):
            raise RecoverySubdraftRejected(
                "Every Session Design child Draft must be explicitly accepted."
            )
        by_target = {targets[item.id]: item for item in drafts if item.id in targets}
        if set(by_target) != set(resolved.redesign_session_ids):
            raise RecoverySubdraftRejected(
                "Session Design child Drafts are incomplete."
            )
        return by_target

    @staticmethod
    def _schedules(
        drafts: tuple[ScheduleDraft, ...], resolved: ResolvedRecoveryActions
    ) -> dict[UUID, _RecoveryScheduleAssignment]:
        by_target: dict[UUID, _RecoveryScheduleAssignment] = {}
        for draft in drafts:
            if (
                draft.status is not ScheduleDraftStatus.ACCEPTED
                or draft.outcome is not ScheduleDraftOutcome.COMPLETE
            ):
                raise RecoverySubdraftRejected(
                    "Every Schedule child Draft must be COMPLETE and accepted."
                )
            for assignment in draft.assignments:
                by_target[assignment.session_id] = _RecoveryScheduleAssignment(
                    draft_id=draft.id,
                    scheduled_start=assignment.scheduled_start,
                    scheduled_end=assignment.scheduled_end,
                )
        if set(by_target) != set(resolved.reschedule_session_ids):
            raise RecoverySubdraftRejected("Schedule child Drafts are incomplete.")
        return by_target


@dataclass(frozen=True, slots=True, kw_only=True)
class _RecoveryScheduleAssignment:
    draft_id: UUID
    scheduled_start: datetime
    scheduled_end: datetime
