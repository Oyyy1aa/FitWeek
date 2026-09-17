"""Deterministic, non-medical weekly execution summary."""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from app.application.errors import BusinessRuleViolation, ResourceNotFound
from app.domain.checkins.models import CheckInStatus
from app.domain.checkins.repositories import CheckInRepository
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.users.models import UserAccount


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanProgressSummary:
    plan_id: UUID
    plan_revision: int
    planned_sessions: int
    completed_sessions: int
    partially_completed_sessions: int
    skipped_sessions: int
    pending_sessions: int
    planned_minutes: int
    actual_minutes: int
    completion_rate: Decimal
    updated_at: datetime


class ProgressService:
    def __init__(self, *, plans: PlanRepository, check_ins: CheckInRepository) -> None:
        self._plans = plans
        self._check_ins = check_ins

    async def summarize(self, user: UserAccount, plan_id: UUID) -> PlanProgressSummary:
        requested = await self._plans.get_for_user(plan_id, user.id)
        if requested is None:
            raise ResourceNotFound("Weekly plan was not found.")
        plan = await self._plans.get_current_confirmed(requested.series_id, user.id)
        if plan is None or plan.status is not WeeklyPlanStatus.CONFIRMED:
            raise BusinessRuleViolation(
                "Progress is available only for a confirmed plan.",
                code="PLAN_NOT_CONFIRMED",
            )
        all_check_ins = await self._check_ins.list_by_series(plan.series_id)
        session_ids = {item.id for item in plan.sessions}
        check_ins = [item for item in all_check_ins if item.session_id in session_ids]
        by_status = {
            status: sum(item.status is status for item in check_ins)
            for status in CheckInStatus
        }
        planned = len(plan.sessions)
        completed = by_status[CheckInStatus.COMPLETED]
        partial = by_status[CheckInStatus.PARTIALLY_COMPLETED]
        skipped = by_status[CheckInStatus.SKIPPED]
        weighted = Decimal(completed) + Decimal("0.5") * Decimal(partial)
        rate = (
            (weighted / Decimal(planned)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            if planned
            else Decimal("0.0000")
        )
        updated_at = max([plan.updated_at, *(item.updated_at for item in check_ins)])
        return PlanProgressSummary(
            plan_id=plan.series_id,
            plan_revision=plan.revision,
            planned_sessions=planned,
            completed_sessions=completed,
            partially_completed_sessions=partial,
            skipped_sessions=skipped,
            pending_sessions=planned - completed - partial - skipped,
            planned_minutes=plan.estimated_total_minutes,
            actual_minutes=sum(item.actual_minutes or 0 for item in check_ins),
            completion_rate=rate,
            updated_at=updated_at,
        )
