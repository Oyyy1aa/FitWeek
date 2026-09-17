"""Create immutable, traceable weekly-plan revision aggregates."""

from datetime import datetime
from typing import Any
from uuid import uuid5

from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.recovery_application.models import RecoveryPlanApplicationMetadata
from app.domain.replanning.models import (
    ChangeImpact,
    PlanChangeMetadata,
    PlanChangeType,
)
from app.domain.schedule_application.models import ScheduleApplicationMetadata
from app.domain.session_design_application.models import (
    SessionDesignApplicationMetadata,
)
from app.domain.sessions.models import WorkoutSession


class PlanRevisionBuilder:
    def build(
        self,
        *,
        source: WeeklyPlan,
        sessions: tuple[WorkoutSession, ...],
        constraint_snapshot: tuple[dict[str, Any], ...],
        change_type: PlanChangeType,
        impact: ChangeImpact,
        client_request_id: str,
        fingerprint: str,
        policy_version: str,
        created_at: datetime,
    ) -> WeeklyPlan:
        revision = source.revision + 1
        revision_id = uuid5(source.series_id, f"revision:{revision}:{fingerprint}")
        metadata = PlanChangeMetadata(
            change_type=change_type,
            source_revision=source.revision,
            changed_session_ids=impact.affected_session_ids,
            preserved_session_ids=impact.preserved_session_ids,
            immutable_session_ids=impact.immutable_session_ids,
            change_fingerprint=fingerprint,
            replanning_policy_version=policy_version,
            client_request_id=client_request_id,
        )
        return WeeklyPlan(
            id=revision_id,
            root_plan_id=source.series_id,
            user_id=source.user_id,
            week_start=source.week_start,
            status=WeeklyPlanStatus.VALIDATED,
            revision=revision,
            parent_revision=source.revision,
            revision_reason=change_type.value,
            change_metadata=metadata,
            goal_snapshot=source.goal_snapshot,
            constraint_snapshot=constraint_snapshot,
            estimated_total_minutes=sum(item.estimated_minutes for item in sessions),
            sessions=sessions,
            created_at=created_at,
            updated_at=created_at,
            confirmed_at=None,
            version=1,
            generation_metadata=source.generation_metadata,
        )

    def build_session_design_application(
        self,
        *,
        source: WeeklyPlan,
        sessions: tuple[WorkoutSession, ...],
        metadata: SessionDesignApplicationMetadata,
        created_at: datetime,
    ) -> WeeklyPlan:
        """Build one Revision that replaces only a Session composition."""

        revision = source.revision + 1
        revision_id = uuid5(
            source.series_id,
            f"revision:{revision}:{metadata.application_fingerprint}",
        )
        return WeeklyPlan(
            id=revision_id,
            root_plan_id=source.series_id,
            user_id=source.user_id,
            week_start=source.week_start,
            status=WeeklyPlanStatus.VALIDATED,
            revision=revision,
            parent_revision=source.revision,
            revision_reason="SESSION_DESIGN_APPLIED",
            change_metadata=metadata,
            goal_snapshot=source.goal_snapshot,
            constraint_snapshot=source.constraint_snapshot,
            estimated_total_minutes=sum(item.estimated_minutes for item in sessions),
            sessions=sessions,
            created_at=created_at,
            updated_at=created_at,
            confirmed_at=None,
            version=1,
            generation_metadata=source.generation_metadata,
        )

    def build_schedule_application(
        self,
        *,
        source: WeeklyPlan,
        sessions: tuple[WorkoutSession, ...],
        metadata: ScheduleApplicationMetadata,
        created_at: datetime,
    ) -> WeeklyPlan:
        """Build a new validated Revision whose only changes are Session times."""

        revision = source.revision + 1
        revision_id = uuid5(
            source.series_id,
            f"revision:{revision}:{metadata.application_fingerprint}",
        )
        return WeeklyPlan(
            id=revision_id,
            root_plan_id=source.series_id,
            user_id=source.user_id,
            week_start=source.week_start,
            status=WeeklyPlanStatus.VALIDATED,
            revision=revision,
            parent_revision=source.revision,
            revision_reason="SCHEDULE_DRAFT_APPLIED",
            change_metadata=metadata,
            goal_snapshot=source.goal_snapshot,
            constraint_snapshot=source.constraint_snapshot,
            estimated_total_minutes=sum(item.estimated_minutes for item in sessions),
            sessions=sessions,
            created_at=created_at,
            updated_at=created_at,
            confirmed_at=None,
            version=1,
            generation_metadata=source.generation_metadata,
        )

    def build_recovery_application(
        self,
        *,
        source: WeeklyPlan,
        sessions: tuple[WorkoutSession, ...],
        metadata: RecoveryPlanApplicationMetadata,
        created_at: datetime,
    ) -> WeeklyPlan:
        """Build one immutable revision for the complete Recovery action set."""

        revision_id = uuid5(
            source.series_id,
            f"revision:{metadata.created_revision}:{metadata.application_fingerprint}",
        )
        return WeeklyPlan(
            id=revision_id,
            root_plan_id=source.series_id,
            user_id=source.user_id,
            week_start=source.week_start,
            status=WeeklyPlanStatus.VALIDATED,
            revision=metadata.created_revision,
            parent_revision=source.revision,
            revision_reason="RECOVERY_DRAFT_APPLIED",
            change_metadata=metadata,
            goal_snapshot=source.goal_snapshot,
            constraint_snapshot=source.constraint_snapshot,
            estimated_total_minutes=sum(item.estimated_minutes for item in sessions),
            sessions=sessions,
            created_at=created_at,
            updated_at=created_at,
            confirmed_at=None,
            version=1,
            generation_metadata=source.generation_metadata,
        )
