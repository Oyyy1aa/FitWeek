"""Pure weekly-plan state transition helpers."""

from datetime import datetime

from app.domain.plans.models import WeeklyPlan


def confirm_plan(
    plan: WeeklyPlan, *, expected_version: int, confirmed_at: datetime
) -> WeeklyPlan:
    """Confirm a validated plan without touching a repository."""

    return plan.confirm(
        expected_version=expected_version,
        confirmed_at=confirmed_at,
    )
