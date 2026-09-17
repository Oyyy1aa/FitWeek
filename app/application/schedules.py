"""Application orchestration for review-only controlled Schedule Drafts."""

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.agents.schedule_agent import ScheduleAgent, ScheduleAgentResult
from app.application.contexts import ContextApplicationService
from app.application.errors import (
    BusinessRuleViolation,
    ResourceNotFound,
    ScheduleDraftExpired,
    ScheduleDraftIdempotencyConflict,
    ScheduleDraftNotFound,
    ScheduleDraftReviewConflict,
    SchedulePlanVersionConflict,
    ScheduleSourcePlanNotFound,
    ScheduleSourceRevisionNotCurrent,
    ScheduleTargetSessionImmutable,
    ScheduleTargetSessionNotFound,
)
from app.calendar_read.gateway import CalendarReadGateway
from app.domain.calendar_read.models import CalendarReadRequest
from app.domain.checkins.repositories import CheckInRepository
from app.domain.common import (
    DomainValidationError,
    RepositoryUniqueError,
)
from app.domain.context.enums import AgentType, ContextSectionName
from app.domain.context.models import ContextBuildCommand
from app.domain.plans.models import WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.profiles.models import ConstraintType
from app.domain.profiles.repositories import ProfileRepository
from app.domain.scheduling.enums import (
    BusyIntervalSource,
    ScheduleDraftOutcome,
    ScheduleDraftSource,
    ScheduleDraftStatus,
)
from app.domain.scheduling.models import (
    AvailabilityWindow,
    BusyInterval,
    BusySnapshot,
    CreateScheduleDraftCommand,
    ScheduleAssignment,
    ScheduleDraft,
    ScheduleTrace,
    TimeSlotCandidateSet,
    UnresolvedSession,
)
from app.domain.scheduling.repositories import ScheduleDraftRepository
from app.domain.sessions.models import WorkoutSessionStatus
from app.domain.users.models import UserAccount
from app.orchestration.clock import Clock
from app.scheduling.busy import build_busy_snapshot
from app.scheduling.candidate_set import TimeSlotCandidateSetBuilder
from app.scheduling.fallback import DeterministicScheduleFallback
from app.scheduling.metrics import ScheduleMetrics
from app.scheduling.time_policy import TimezonePolicy
from app.scheduling.validator import ScheduleAgentBusinessValidator


class ScheduleDraftService:
    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        plans: PlanRepository,
        check_ins: CheckInRepository,
        drafts: ScheduleDraftRepository,
        contexts: ContextApplicationService,
        calendar: CalendarReadGateway,
        agent: ScheduleAgent,
        clock: Clock,
        metrics: ScheduleMetrics,
        enabled: bool,
        builder: TimeSlotCandidateSetBuilder,
        ttl_minutes: int = 30,
    ) -> None:
        self._profiles = profiles
        self._plans = plans
        self._check_ins = check_ins
        self._drafts = drafts
        self._contexts = contexts
        self._calendar = calendar
        self._agent = agent
        self._clock = clock
        self._metrics = metrics
        self._enabled = enabled
        self._builder = builder
        self._ttl = timedelta(minutes=ttl_minutes)
        self._fallback = DeterministicScheduleFallback()
        self._validator = ScheduleAgentBusinessValidator()
        self._create_lock = asyncio.Lock()

    async def create(
        self, user: UserAccount, command: CreateScheduleDraftCommand
    ) -> tuple[ScheduleDraft, bool]:
        async with self._create_lock:
            return await self._create_serialized(user, command)

    async def _create_serialized(
        self, user: UserAccount, command: CreateScheduleDraftCommand
    ) -> tuple[ScheduleDraft, bool]:
        self._metrics.schedule_draft_requests += 1
        now = self._clock.now()
        plan = await self._plans.get_revision_for_user(
            command.root_plan_id, user.id, command.source_revision
        )
        if plan is None:
            raise ScheduleSourcePlanNotFound(
                "The requested Plan revision was not found."
            )
        current = await self._plans.get_current_confirmed(command.root_plan_id, user.id)
        if (
            current is None
            or current.id != plan.id
            or plan.status is not WeeklyPlanStatus.CONFIRMED
        ):
            raise ScheduleSourceRevisionNotCurrent(
                "Only the current CONFIRMED Plan revision can be scheduled."
            )
        if plan.version != command.expected_plan_version:
            self._metrics.schedule_conflicts_rejected += 1
            raise SchedulePlanVersionConflict("The Plan version changed.")
        profile = await self._profiles.get_by_user_id(user.id)
        if profile is None:
            raise ResourceNotFound("A Fitness Profile is required.")
        constraints = tuple(await self._profiles.list_constraints(profile.id))
        self._validate_windows(command, plan.week_start, now)
        availability = self._normalize_availability(command.availability_windows)
        payload_fingerprint = self._payload_fingerprint(
            command, plan.id, plan.version, availability
        )
        existing = await self._drafts.get_by_request(user.id, command.client_request_id)
        if existing is not None:
            if existing.request_payload_fingerprint != payload_fingerprint:
                self._metrics.schedule_idempotency_conflicts += 1
                raise ScheduleDraftIdempotencyConflict(
                    "The client request ID was already used with different inputs."
                )
            self._metrics.schedule_idempotent_reuses += 1
            return existing, True

        check_ins = tuple(await self._check_ins.list_by_series(plan.series_id))
        checked = {item.session_id for item in check_ins}
        requested = set(command.target_session_ids or ())
        if requested - {item.id for item in plan.sessions}:
            raise ScheduleTargetSessionNotFound(
                "One or more target Sessions were not found."
            )
        immutable = tuple(
            item
            for item in plan.sessions
            if item.id in checked
            or item.scheduled_start <= now
            or item.status is not WorkoutSessionStatus.PLANNED
        )
        immutable_ids = {item.id for item in immutable}
        sessions = tuple(
            item
            for item in plan.sessions
            if (item.id in requested if requested else item.id not in immutable_ids)
        )
        invalid_targets = requested & immutable_ids
        if invalid_targets:
            raise ScheduleTargetSessionImmutable(
                "Only future, PLANNED, unchecked Sessions can be scheduled."
            )
        if not sessions:
            raise BusinessRuleViolation(
                "No eligible future Sessions are available.",
                code="SCHEDULE_NO_FEASIBLE_SLOT",
            )

        week_start, week_end = TimezonePolicy().week_bounds(
            plan.week_start, command.timezone
        )
        calendar_result = await self._calendar.read_busy(
            CalendarReadRequest(
                start=max(week_start, now), end=week_end, timezone=command.timezone
            )
        )
        if calendar_result.mode.value != "DISABLED":
            self._metrics.calendar_read_requests += 1
        if calendar_result.mode.value == "PROVIDER":
            self._metrics.calendar_read_successes += 1
        elif calendar_result.mode.value == "MANUAL_ONLY":
            self._metrics.calendar_read_failures += 1
            self._metrics.calendar_manual_degraded += 1
        constraint_busy: list[BusyInterval] = []
        for constraint in constraints:
            if (
                constraint.constraint_type is ConstraintType.UNAVAILABLE_TIME
                and constraint.is_hard
                and (constraint.valid_until is None or constraint.valid_until > now)
            ):
                try:
                    raw_start, raw_end = constraint.constraint_value.split(
                        "/", maxsplit=1
                    )
                    start = datetime.fromisoformat(raw_start)
                    end = datetime.fromisoformat(raw_end)
                    if start.tzinfo is not None and end.tzinfo is not None:
                        constraint_busy.append(
                            BusyInterval(
                                start=start.astimezone(UTC),
                                end=end.astimezone(UTC),
                                source=BusyIntervalSource.MANUAL,
                            )
                        )
                except (ValueError, DomainValidationError):
                    continue
        busy = build_busy_snapshot(
            user_id=user.id,
            timezone=command.timezone,
            mode=calendar_result.mode,
            provider_intervals=calendar_result.intervals,
            manual_intervals=command.manual_busy_windows + tuple(constraint_busy),
            provider_summary=calendar_result.provider_summary,
            created_at=now,
            range_start_utc=max(week_start, now),
            range_end_utc=week_end,
        )
        context = await self._contexts.build_snapshot(
            user,
            ContextBuildCommand(
                agent_type=AgentType.SCHEDULE_AGENT,
                current_task={
                    "request_type": "controlled_schedule_draft",
                    "root_plan_id": str(plan.series_id),
                    "source_revision": str(plan.revision),
                    "timezone": command.timezone,
                    "busy_snapshot": busy.fingerprint,
                },
                plan_id=plan.id,
            ),
            scope_id=(
                "schedule:"
                + self._combined_fingerprint(
                    str(user.id),
                    command.client_request_id,
                    payload_fingerprint,
                )
            ),
        )
        try:
            memory_items = {
                item.key.casefold(): item.value.casefold()
                for section in context.context.sections
                if section.name is ContextSectionName.RELEVANT_CONFIRMED_MEMORIES
                for item in section.items
            }
            candidate_set = self._builder.build(
                user_id=user.id,
                root_plan_id=plan.series_id,
                source_revision=plan.revision,
                sessions=sessions,
                availability=availability,
                busy=busy,
                immutable_sessions=tuple(
                    item for item in plan.sessions if item not in sessions
                ),
                context=context.reference,
                created_at=now,
                preferred_times=tuple(
                    value for key, value in memory_items.items() if "time" in key
                ),
                preferred_locations=tuple(
                    value for key, value in memory_items.items() if "location" in key
                ),
            )
        except DomainValidationError as exc:
            raise BusinessRuleViolation(
                str(exc), code="SCHEDULE_CANDIDATE_SET_EMPTY"
            ) from exc
        request_fingerprint = self._combined_fingerprint(
            str(user.id),
            payload_fingerprint,
            busy.fingerprint,
            context.reference.context_fingerprint,
            candidate_set.fingerprint,
            "schedule-agent-v1",
            "schedule-draft-policy-v1",
        )
        if self._enabled:
            result = await self._agent.run(
                user_id=user.id,
                request_fingerprint=request_fingerprint,
                candidate_set=candidate_set,
                context=context,
            )
        else:
            result = ScheduleAgentResult(
                request_id=uuid4(),
                output=self._fallback.build(candidate_set),
                prompt_version="schedule-agent-v1",
                provider_summary="gateway-disabled:deterministic-fallback",
                fallback_used=True,
                provider_attempts=0,
            )
        output = self._validator.validate(result.output, candidate_set)
        slot_map = candidate_set.slot_map
        assignments = tuple(
            ScheduleAssignment(
                session_id=item.session_id,
                slot_id=item.slot_id,
                scheduled_start=slot_map[item.slot_id].start,
                scheduled_end=slot_map[item.slot_id].end,
                location=slot_map[item.slot_id].location,
            )
            for item in output.assignments
        )
        unresolved = tuple(
            UnresolvedSession(
                session_id=session_id,
                code="NO_NON_OVERLAPPING_SLOT",
                message="No legal non-overlapping frozen Slot could be assigned.",
            )
            for session_id in output.unresolved_session_ids
        )
        outcome = (
            ScheduleDraftOutcome.COMPLETE
            if not unresolved
            else ScheduleDraftOutcome.PARTIAL
        )
        source = (
            ScheduleDraftSource.DETERMINISTIC_FALLBACK
            if result.fallback_used
            else ScheduleDraftSource.MODEL
        )
        draft_id = uuid5(
            NAMESPACE_URL,
            f"fitweek:schedule-draft:{user.id}:{command.client_request_id}:{request_fingerprint}",
        )
        draft = ScheduleDraft(
            id=draft_id,
            request_id=result.request_id,
            client_request_id=command.client_request_id,
            user_id=user.id,
            request_payload_fingerprint=payload_fingerprint,
            request_fingerprint=request_fingerprint,
            root_plan_id=plan.series_id,
            source_revision=plan.revision,
            source_plan_version=plan.version,
            timezone=command.timezone,
            busy_snapshot_id=busy.id,
            candidate_set_id=candidate_set.id,
            candidate_set_fingerprint=candidate_set.fingerprint,
            context_snapshot_reference_id=context.reference.id,
            context_fingerprint=context.reference.context_fingerprint,
            context_degraded_mode=context.reference.degraded_mode,
            assignments=assignments,
            unresolved=unresolved,
            outcome=outcome,
            source=source,
            prompt_version=result.prompt_version,
            provider_summary=result.provider_summary,
            fallback_used=result.fallback_used,
            calendar_verification_status=busy.verification_status,
            explanation_summary=output.explanation_summary,
            created_at=now,
            expires_at=now + self._ttl,
        )
        traces = (
            await self._agent.list_traces(user_id=user.id, request_id=result.request_id)
            if self._enabled
            else ()
        )
        trace = ScheduleTrace(
            draft_id=draft.id,
            request_id=draft.request_id,
            busy_snapshot_id=busy.id,
            candidate_set_id=candidate_set.id,
            candidate_set_fingerprint=candidate_set.fingerprint,
            context_snapshot_reference_id=context.reference.id,
            context_fingerprint=context.reference.context_fingerprint,
            prompt_version=result.prompt_version,
            provider_summary=result.provider_summary,
            timezone=command.timezone,
            provider_name=result.provider_summary.split(":", maxsplit=1)[0],
            provider_version=(
                result.provider_summary.split(":", maxsplit=2)[1]
                if ":" in result.provider_summary
                else "none"
            ),
            attempt_no=result.provider_attempts,
            outcome="FALLBACK" if result.fallback_used else "SUCCESS",
            validation_error_code=None,
            latency_ms=0,
            source=source,
            fallback_used=result.fallback_used,
            provider_attempts=result.provider_attempts,
            calendar_mode=busy.mode,
            calendar_attempts=calendar_result.attempts,
            model_trace_ids=tuple(item.id for item in traces),
            created_at=now,
        )
        try:
            saved = await self._drafts.save(
                draft,
                busy,
                candidate_set,
                trace,
                availability,
            )
        except RepositoryUniqueError as exc:
            concurrent = await self._drafts.get_by_request(
                user.id, command.client_request_id
            )
            if (
                concurrent is not None
                and concurrent.request_payload_fingerprint == payload_fingerprint
            ):
                self._metrics.schedule_idempotent_reuses += 1
                return concurrent, True
            raise ScheduleDraftIdempotencyConflict(
                "The client request ID conflicts with another Schedule Draft."
            ) from exc
        if source is ScheduleDraftSource.MODEL:
            if "backup" in result.provider_summary:
                self._metrics.schedule_agent_backup_successes += 1
            else:
                self._metrics.schedule_agent_primary_successes += 1
        else:
            self._metrics.schedule_deterministic_fallbacks += 1
        if outcome is ScheduleDraftOutcome.PARTIAL:
            self._metrics.schedule_partial_drafts += 1
        else:
            self._metrics.schedule_complete_drafts += 1
        return saved, False

    async def get(self, user: UserAccount, draft_id: UUID) -> ScheduleDraft:
        draft = await self._drafts.get_draft(user.id, draft_id)
        if draft is None:
            raise ScheduleDraftNotFound("Schedule Draft was not found.")
        now = self._clock.now()
        if (
            draft.status is ScheduleDraftStatus.PENDING_REVIEW
            and draft.expires_at <= now
        ):
            draft = await self._drafts.update(draft.expire(now))
        return draft

    async def review(
        self,
        user: UserAccount,
        draft_id: UUID,
        *,
        expected_version: int,
        accept: bool,
    ) -> ScheduleDraft:
        draft = await self.get(user, draft_id)
        if draft.status is ScheduleDraftStatus.EXPIRED:
            raise ScheduleDraftExpired("Schedule Draft has expired.")
        if (
            draft.version != expected_version
            or draft.status is not ScheduleDraftStatus.PENDING_REVIEW
        ):
            raise ScheduleDraftReviewConflict("The Draft version or state changed.")
        try:
            reviewed = (
                draft.accept(self._clock.now())
                if accept
                else draft.reject(self._clock.now())
            )
        except DomainValidationError as exc:
            raise BusinessRuleViolation(
                str(exc), code="SCHEDULE_DRAFT_INCOMPLETE"
            ) from exc
        saved = await self._drafts.update(reviewed)
        if accept:
            self._metrics.schedule_drafts_accepted += 1
        else:
            self._metrics.schedule_drafts_rejected += 1
        return saved

    async def busy_snapshot(self, user: UserAccount, draft_id: UUID) -> BusySnapshot:
        await self.get(user, draft_id)
        value = await self._drafts.get_busy_snapshot(user.id, draft_id)
        if value is None:
            raise ScheduleDraftNotFound("Busy Snapshot was not found.")
        return value

    async def candidate_set(
        self, user: UserAccount, draft_id: UUID
    ) -> TimeSlotCandidateSet:
        await self.get(user, draft_id)
        value = await self._drafts.get_candidate_set(user.id, draft_id)
        if value is None:
            raise ScheduleDraftNotFound("Time Slot Candidate Set was not found.")
        return value

    async def trace(self, user: UserAccount, draft_id: UUID) -> ScheduleTrace:
        await self.get(user, draft_id)
        value = await self._drafts.get_trace(user.id, draft_id)
        if value is None:
            raise ScheduleDraftNotFound("Schedule trace was not found.")
        return value

    def metrics(self) -> dict[str, int]:
        return self._metrics.as_dict()

    @staticmethod
    def _validate_windows(
        command: CreateScheduleDraftCommand,
        week_start_date: date,
        now: datetime,
    ) -> None:
        week_start, week_end = TimezonePolicy().week_bounds(
            week_start_date,
            command.timezone,
        )
        windows = sorted(command.availability_windows, key=lambda item: item.start)
        for item in windows:
            if item.start < now or item.start < week_start or item.end > week_end:
                raise BusinessRuleViolation(
                    "Availability must be future time inside the target week.",
                    code="SCHEDULE_AVAILABILITY_INVALID",
                )

    @staticmethod
    def _normalize_availability(
        windows: tuple[AvailabilityWindow, ...],
    ) -> tuple[AvailabilityWindow, ...]:
        merged: list[AvailabilityWindow] = []
        for item in sorted(
            windows, key=lambda value: (value.location, value.start, value.end)
        ):
            if (
                merged
                and merged[-1].location is item.location
                and item.start <= merged[-1].end
            ):
                previous = merged[-1]
                merged[-1] = AvailabilityWindow(
                    start=previous.start,
                    end=max(previous.end, item.end),
                    location=item.location,
                )
            else:
                merged.append(item)
        return tuple(sorted(merged, key=lambda value: (value.start, value.location)))

    @staticmethod
    def _payload_fingerprint(
        command: CreateScheduleDraftCommand,
        plan_id: UUID,
        plan_version: int,
        availability: tuple[AvailabilityWindow, ...],
    ) -> str:
        payload = {
            "client_request_id": command.client_request_id,
            "root_plan_id": str(command.root_plan_id),
            "plan_id": str(plan_id),
            "source_revision": command.source_revision,
            "plan_version": plan_version,
            "timezone": command.timezone,
            "availability": [
                (item.start.isoformat(), item.end.isoformat(), item.location.value)
                for item in availability
            ],
            "manual_busy": [
                (item.start.isoformat(), item.end.isoformat())
                for item in command.manual_busy_windows
            ],
            "targets": [str(item) for item in command.target_session_ids or ()],
            "policy": "schedule-draft-v1",
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _combined_fingerprint(*values: str) -> str:
        return hashlib.sha256(":".join(values).encode()).hexdigest()
