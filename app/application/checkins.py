"""Training check-in use cases with immutable idempotency semantics."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from app.application.errors import (
    BusinessRuleViolation,
    CheckInNotFound,
    IdempotencyConflict,
    ResourceNotFound,
    SessionAlreadyCheckedIn,
)
from app.domain.checkins.models import CheckInStatus, WorkoutCheckIn
from app.domain.checkins.repositories import CheckInRepository
from app.domain.common import DomainValidationError, RepositoryUniqueError
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.users.models import UserAccount


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateCheckInCommand:
    client_event_id: str
    status: CheckInStatus
    actual_minutes: int | None
    perceived_effort: int | None
    note: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckInCreationResult:
    check_in: WorkoutCheckIn
    created: bool


class CheckInService:
    def __init__(self, *, plans: PlanRepository, check_ins: CheckInRepository) -> None:
        self._plans = plans
        self._check_ins = check_ins

    async def create(
        self,
        user: UserAccount,
        session_id: UUID,
        command: CreateCheckInCommand,
    ) -> CheckInCreationResult:
        plan = await self._plans.get_current_plan_for_session(session_id, user.id)
        if plan is None:
            raise ResourceNotFound("Workout session was not found.")
        if plan.status is not WeeklyPlanStatus.CONFIRMED:
            raise BusinessRuleViolation(
                "Only a session in a confirmed plan can be checked in.",
                code="SESSION_NOT_CONFIRMED",
            )
        session = next((item for item in plan.sessions if item.id == session_id), None)
        if session is None:
            raise ResourceNotFound("Workout session was not found.")
        try:
            candidate = WorkoutCheckIn(
                id=uuid5(user.id, f"check-in:{command.client_event_id.strip()}"),
                client_event_id=command.client_event_id,
                user_id=user.id,
                plan_id=plan.series_id,
                plan_revision=plan.revision,
                session_id=session.id,
                status=command.status,
                actual_minutes=command.actual_minutes,
                perceived_effort=command.perceived_effort,
                note=command.note,
                occurred_at=command.occurred_at,
                created_at=command.occurred_at,
                updated_at=command.occurred_at,
                version=1,
            )
        except DomainValidationError as exc:
            raise BusinessRuleViolation(str(exc), code="INVALID_CHECK_IN") from exc

        existing = await self._check_ins.get_by_client_event_id(
            user.id, candidate.client_event_id
        )
        if existing is not None:
            if existing.same_event_payload(candidate):
                return CheckInCreationResult(check_in=existing, created=False)
            raise IdempotencyConflict(
                "client_event_id was already used with a different payload."
            )
        session_check_in = await self._check_ins.get_by_session_id(session.id)
        if session_check_in is not None:
            raise SessionAlreadyCheckedIn(
                "The session already has an immutable check-in."
            )
        try:
            saved = await self._check_ins.save(candidate)
        except RepositoryUniqueError as exc:
            if exc.constraint == "check_in.user_client_event":
                concurrent = await self._check_ins.get_by_client_event_id(
                    user.id, candidate.client_event_id
                )
                if concurrent is not None and concurrent.same_event_payload(candidate):
                    return CheckInCreationResult(check_in=concurrent, created=False)
                raise IdempotencyConflict(
                    "client_event_id was concurrently used with another payload."
                ) from exc
            raise SessionAlreadyCheckedIn(
                "The session already has an immutable check-in."
            ) from exc
        return CheckInCreationResult(check_in=saved, created=True)

    async def get_for_session(
        self, user: UserAccount, session_id: UUID
    ) -> WorkoutCheckIn:
        plan = await self._plans.get_current_plan_for_session(session_id, user.id)
        if plan is None:
            raise ResourceNotFound("Workout session was not found.")
        check_in = await self._check_ins.get_by_session_id(session_id)
        if check_in is None or check_in.user_id != user.id:
            raise CheckInNotFound("Check-in was not found.")
        return check_in
