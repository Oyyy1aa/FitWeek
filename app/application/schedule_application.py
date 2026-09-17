"""Apply an accepted COMPLETE Schedule Draft as one validated Plan Revision."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from app.application.errors import (
    ResourceNotFound,
    ScheduleApplicationNotFound,
    ScheduleApplyIdempotencyConflict,
    ScheduleCalendarRevalidationFailed,
    ScheduleDraftAlreadyApplied,
    ScheduleDraftExpired,
    ScheduleDraftIncomplete,
    ScheduleDraftNotAccepted,
    ScheduleDraftVersionConflict,
    ScheduleNewCalendarConflict,
    SchedulePlanApplicationFailed,
    SchedulePlanSafetyFailed,
    SchedulePlanVersionConflict,
    ScheduleSourcePlanNotFound,
    ScheduleSourceRevisionNotCurrent,
    ScheduleTargetSessionCheckedIn,
    ScheduleTargetSessionImmutable,
    ScheduleTargetSessionNotFound,
)
from app.calendar_read.gateway import CalendarReadGateway
from app.domain.calendar_read.models import CalendarReadRequest
from app.domain.checkins.models import WorkoutCheckIn
from app.domain.checkins.repositories import CheckInRepository
from app.domain.common import RepositoryConflictError, RepositoryUniqueError
from app.domain.context.repositories import ContextSnapshotRepository
from app.domain.exercises.models import Exercise
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.domain.profiles.repositories import ProfileRepository
from app.domain.schedule_application.models import (
    ApplyScheduleDraftCommand,
    ScheduleApplicationResult,
    ScheduleApplyOutcome,
    ScheduleApplyPreview,
    ScheduleApplyValidation,
    ScheduleAssignmentChange,
)
from app.domain.schedule_application.repositories import ScheduleApplicationRepository
from app.domain.scheduling.enums import (
    CalendarReadMode,
    CalendarVerificationStatus,
    ScheduleDraftOutcome,
    ScheduleDraftStatus,
)
from app.domain.scheduling.models import (
    BusySnapshot,
    ScheduleDraft,
    TimeSlotCandidateSet,
)
from app.domain.scheduling.repositories import ScheduleDraftRepository
from app.domain.sessions.models import WorkoutSessionStatus
from app.domain.users.models import UserAccount
from app.orchestration.clock import Clock
from app.safety.engine import SafetyEngine
from app.schedule_application.merge_policy import SchedulePlanMergePolicy
from app.scheduling.time_policy import overlaps


@dataclass(frozen=True, slots=True, kw_only=True)
class _Prepared:
    source: WeeklyPlan
    draft: ScheduleDraft
    candidate_set: TimeSlotCandidateSet
    busy: BusySnapshot
    revision: WeeklyPlan
    preview: ScheduleApplyPreview


class SchedulePlanApplicationService:
    """Read/merge/Safety outside the lock and CAS commit inside the repository."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        exercises: ExerciseRepository,
        plans: PlanRepository,
        check_ins: CheckInRepository,
        drafts: ScheduleDraftRepository,
        contexts: ContextSnapshotRepository,
        applications: ScheduleApplicationRepository,
        calendar: CalendarReadGateway,
        safety: SafetyEngine,
        clock: Clock,
        busy_snapshot_max_age_seconds: int = 300,
        merge_policy: SchedulePlanMergePolicy | None = None,
    ) -> None:
        self._profiles = profiles
        self._exercises = exercises
        self._plans = plans
        self._check_ins = check_ins
        self._drafts = drafts
        self._contexts = contexts
        self._applications = applications
        self._calendar = calendar
        self._safety = safety
        self._clock = clock
        self._max_busy_age = busy_snapshot_max_age_seconds
        self._merge = merge_policy or SchedulePlanMergePolicy()

    async def preview(
        self, user: UserAccount, draft_id: UUID, command: ApplyScheduleDraftCommand
    ) -> ScheduleApplyPreview:
        return (await self._prepare(user, draft_id, command)).preview

    async def apply(
        self, user: UserAccount, draft_id: UUID, command: ApplyScheduleDraftCommand
    ) -> ScheduleApplyOutcome:
        existing = await self._applications.get_result_by_request(
            user.id, command.client_request_id
        )
        if existing is not None:
            fingerprint = await self._fingerprint_for_existing(
                user, draft_id, command, existing.schedule_draft_id
            )
            if existing.application_fingerprint != fingerprint:
                raise ScheduleApplyIdempotencyConflict(
                    "The client request ID was used with different Schedule inputs."
                )
            return await self._outcome(user, existing, created=False)
        prepared = await self._prepare(user, draft_id, command)
        now = self._clock.now()
        result_id = uuid5(
            NAMESPACE_URL,
            f"fitweek:schedule-application:{user.id}:{command.client_request_id}:"
            f"{prepared.preview.application_fingerprint}",
        )
        result = ScheduleApplicationResult(
            id=result_id,
            user_id=user.id,
            client_request_id=command.client_request_id,
            application_fingerprint=prepared.preview.application_fingerprint,
            schedule_draft_id=prepared.draft.id,
            root_plan_id=prepared.source.series_id,
            source_revision=prepared.source.revision,
            created_revision=prepared.revision.revision,
            previous_plan_version=prepared.source.version,
            resulting_plan_version=prepared.revision.version,
            changed_session_ids=tuple(
                item.session_id for item in prepared.draft.assignments
            ),
            calendar_verification_status=(
                prepared.preview.validation.calendar_verification_status
            ),
            created_at=now,
        )
        applied = prepared.draft.mark_applied(
            root_plan_id=prepared.source.series_id,
            source_revision=prepared.source.revision,
            created_revision=prepared.revision.revision,
            application_result_id=result.id,
            at=now,
        )
        try:
            committed = await self._applications.commit(
                source=prepared.source,
                expected_draft=prepared.draft,
                applied_draft=applied,
                revision=prepared.revision,
                result=result,
            )
        except RepositoryUniqueError as exc:
            concurrent = await self._applications.get_result_by_request(
                user.id, command.client_request_id
            )
            if (
                concurrent is not None
                and concurrent.application_fingerprint
                == prepared.preview.application_fingerprint
            ):
                return await self._outcome(user, concurrent, created=False)
            if exc.constraint.endswith("user_request"):
                raise ScheduleApplyIdempotencyConflict(
                    "The client request ID conflicts with another application."
                ) from exc
            if exc.constraint.endswith("draft"):
                raise ScheduleDraftAlreadyApplied(
                    "The Schedule Draft was already applied."
                ) from exc
            raise SchedulePlanVersionConflict(
                "The next Plan Revision is already occupied."
            ) from exc
        except RepositoryConflictError as exc:
            if exc.resource == "ScheduleDraft":
                raise ScheduleDraftVersionConflict(
                    "The Schedule Draft changed before commit."
                ) from exc
            raise SchedulePlanVersionConflict(
                "The current Plan Revision changed before commit."
            ) from exc
        except RuntimeError as exc:
            raise SchedulePlanApplicationFailed(
                "The Schedule application could not be committed."
            ) from exc
        return await self._outcome(user, committed.result, created=committed.created)

    async def get_result(
        self, user: UserAccount, draft_id: UUID
    ) -> ScheduleApplyOutcome:
        result = await self._applications.get_result_by_draft(user.id, draft_id)
        if result is None:
            raise ScheduleApplicationNotFound(
                "Schedule application result was not found."
            )
        return await self._outcome(user, result, created=False)

    async def _prepare(
        self, user: UserAccount, draft_id: UUID, command: ApplyScheduleDraftCommand
    ) -> _Prepared:
        now = self._clock.now()
        draft = await self._drafts.get_draft(user.id, draft_id)
        if draft is None:
            raise ScheduleSourcePlanNotFound("The Schedule Draft was not found.")
        self._validate_draft(draft, command, now)
        candidate = await self._drafts.get_candidate_set(user.id, draft.id)
        busy = await self._drafts.get_busy_snapshot(user.id, draft.id)
        context = await self._contexts.get(user.id, draft.context_snapshot_reference_id)
        if candidate is None or busy is None or context is None:
            raise ScheduleDraftNotAccepted(
                "Frozen Schedule Draft references are unavailable."
            )
        if (
            candidate.fingerprint != draft.candidate_set_fingerprint
            or context.reference.context_fingerprint != draft.context_fingerprint
        ):
            raise ScheduleDraftNotAccepted("Frozen Schedule Draft references changed.")
        source = await self._load_source(user.id, command)
        check_ins = tuple(await self._check_ins.list_by_series(source.series_id))
        self._validate_assignments(draft, candidate, source, check_ins, now)
        age = max(0, int((now - busy.created_at).total_seconds()))
        revalidated = False
        verification = draft.calendar_verification_status
        if (
            verification is CalendarVerificationStatus.VERIFIED
            and age > self._max_busy_age
        ):
            result = await self._calendar.read_busy(
                CalendarReadRequest(
                    start=busy.range_start_utc,
                    end=busy.range_end_utc,
                    timezone=busy.timezone,
                )
            )
            if result.mode is not CalendarReadMode.PROVIDER:
                self._calendar.record_stale_rejection(
                    user_id=user.id,
                    code="SCHEDULE_CALENDAR_REVALIDATION_FAILED",
                )
                raise ScheduleCalendarRevalidationFailed(
                    "A stale verified Busy Snapshot could not be refreshed."
                )
            revalidated = True
            for assignment in draft.assignments:
                if any(
                    overlaps(
                        assignment.scheduled_start,
                        assignment.scheduled_end,
                        item.start,
                        item.end,
                    )
                    for item in result.intervals
                ):
                    self._calendar.record_stale_rejection(
                        user_id=user.id,
                        code="SCHEDULE_NEW_CALENDAR_CONFLICT",
                    )
                    raise ScheduleNewCalendarConflict(
                        "A new Calendar conflict blocks Schedule application."
                    )
        fingerprint = self._fingerprint(
            user_id=user.id,
            draft=draft,
            source=source,
            candidate=candidate,
            busy=busy,
            command=command,
        )
        revision, metadata = self._merge.build(
            source=source,
            draft=draft,
            check_ins=check_ins,
            fingerprint=fingerprint,
            created_at=now,
        )
        profile, constraints = await self._profile_context(user.id)
        catalog = await self._load_catalog(revision)
        validation = self._safety.validate_plan(
            profile=profile,
            constraints=constraints,
            plan=revision,
            exercise_catalog=catalog,
        )
        if not validation.passed:
            raise SchedulePlanSafetyFailed(
                "The scheduled Plan Revision failed the complete Safety gate.",
                violations=validation.violations,
            )
        timezone = ZoneInfo(draft.timezone)
        source_by_id = {item.id: item for item in source.sessions}
        changes = tuple(
            ScheduleAssignmentChange(
                session_id=item.session_id,
                old_start_utc=source_by_id[item.session_id].scheduled_start,
                old_end_utc=source_by_id[item.session_id].scheduled_end,
                new_start_utc=item.scheduled_start,
                new_end_utc=item.scheduled_end,
                timezone=draft.timezone,
                old_start_local=source_by_id[
                    item.session_id
                ].scheduled_start.astimezone(timezone),
                new_start_local=item.scheduled_start.astimezone(timezone),
                duration_seconds=int(
                    (
                        source_by_id[item.session_id].scheduled_end
                        - source_by_id[item.session_id].scheduled_start
                    ).total_seconds()
                ),
                location_type=source_by_id[item.session_id].location_type,
            )
            for item in draft.assignments
        )
        preview = ScheduleApplyPreview(
            draft_id=draft.id,
            draft_version=draft.version,
            root_plan_id=source.series_id,
            source_revision=source.revision,
            source_plan_version=source.version,
            assignment_changes=changes,
            preserved_session_ids=metadata.preserved_session_ids,
            immutable_session_ids=metadata.immutable_session_ids,
            validation=ScheduleApplyValidation(
                passed=True,
                violations=(),
                busy_snapshot_age_seconds=age,
                calendar_verification_status=verification,
                calendar_revalidated=revalidated,
            ),
            application_fingerprint=fingerprint,
        )
        return _Prepared(
            source=source,
            draft=draft,
            candidate_set=candidate,
            busy=busy,
            revision=revision,
            preview=preview,
        )

    @staticmethod
    def _validate_draft(
        draft: ScheduleDraft, command: ApplyScheduleDraftCommand, now: datetime
    ) -> None:
        if draft.status is ScheduleDraftStatus.APPLIED:
            raise ScheduleDraftAlreadyApplied("The Schedule Draft was already applied.")
        if draft.status is ScheduleDraftStatus.EXPIRED or draft.expires_at <= now:
            raise ScheduleDraftExpired("The Schedule Draft has expired.")
        if draft.version != command.expected_draft_version:
            raise ScheduleDraftVersionConflict("The Schedule Draft version is stale.")
        if draft.outcome is not ScheduleDraftOutcome.COMPLETE:
            raise ScheduleDraftIncomplete("A PARTIAL Schedule Draft cannot be applied.")
        if draft.status is not ScheduleDraftStatus.ACCEPTED:
            raise ScheduleDraftNotAccepted(
                "Only an ACCEPTED Schedule Draft can be applied."
            )

    async def _load_source(
        self, user_id: UUID, command: ApplyScheduleDraftCommand
    ) -> WeeklyPlan:
        source = await self._plans.get_revision_for_user(
            command.root_plan_id, user_id, command.source_revision
        )
        if source is None:
            raise ScheduleSourcePlanNotFound("The source Plan Revision was not found.")
        current = await self._plans.get_current_confirmed(command.root_plan_id, user_id)
        if (
            source.status is not WeeklyPlanStatus.CONFIRMED
            or current is None
            or current.id != source.id
        ):
            raise ScheduleSourceRevisionNotCurrent(
                "Only the current CONFIRMED Revision can be scheduled."
            )
        if source.version != command.expected_plan_version:
            raise SchedulePlanVersionConflict("The Plan version is stale.")
        revisions = await self._plans.list_revisions_for_user(
            command.root_plan_id, user_id
        )
        if any(item.revision > source.revision for item in revisions):
            raise SchedulePlanVersionConflict(
                "A newer pending Plan Revision already exists."
            )
        return source

    @staticmethod
    def _validate_assignments(
        draft: ScheduleDraft,
        candidate: TimeSlotCandidateSet,
        source: WeeklyPlan,
        check_ins: tuple[WorkoutCheckIn, ...],
        now: datetime,
    ) -> None:
        source_by_id = {item.id: item for item in source.sessions}
        checked = {item.session_id for item in check_ins}
        slot_map = candidate.slot_map
        if {item.session_id for item in draft.assignments} != {
            item.session_id for item in candidate.session_candidates
        }:
            raise ScheduleDraftIncomplete(
                "The Schedule Draft does not account for every target Session."
            )
        for assignment in draft.assignments:
            session = source_by_id.get(assignment.session_id)
            if session is None:
                raise ScheduleTargetSessionNotFound(
                    "A scheduled Session is absent from the source Revision."
                )
            if session.id in checked:
                raise ScheduleTargetSessionCheckedIn(
                    "A checked-in Session cannot be rescheduled."
                )
            if (
                session.status is not WorkoutSessionStatus.PLANNED
                or session.scheduled_start <= now
            ):
                raise ScheduleTargetSessionImmutable(
                    "Only a future PLANNED Session can be rescheduled."
                )
            slot = slot_map.get(assignment.slot_id)
            if (
                slot is None
                or slot.session_id != session.id
                or slot.start != assignment.scheduled_start
                or slot.end != assignment.scheduled_end
                or slot.location is not session.location_type
                or (slot.end - slot.start)
                != (session.scheduled_end - session.scheduled_start)
            ):
                raise ScheduleDraftNotAccepted(
                    "An assignment no longer matches the frozen Candidate Set."
                )

    async def _profile_context(
        self, user_id: UUID
    ) -> tuple[FitnessProfile, tuple[UserConstraint, ...]]:
        profile = await self._profiles.get_by_user_id(user_id)
        if profile is None:
            raise ResourceNotFound("Fitness Profile was not found.")
        return profile, tuple(await self._profiles.list_constraints(profile.id))

    async def _load_catalog(self, plan: WeeklyPlan) -> dict[str, Exercise]:
        catalog = {item.id: item for item in await self._exercises.list_active()}
        for exercise_id in sorted(
            {
                item.exercise_id
                for session in plan.sessions
                for item in session.exercises
            }
        ):
            if exercise_id not in catalog:
                exercise = await self._exercises.get(exercise_id)
                if exercise is not None:
                    catalog[exercise_id] = exercise
        return catalog

    async def _outcome(
        self,
        user: UserAccount,
        result: ScheduleApplicationResult,
        *,
        created: bool,
    ) -> ScheduleApplyOutcome:
        plan = await self._plans.get_revision_for_user(
            result.root_plan_id, user.id, result.created_revision
        )
        draft = await self._drafts.get_draft(user.id, result.schedule_draft_id)
        if plan is None or draft is None:
            raise ScheduleApplicationNotFound(
                "Schedule application audit references are incomplete."
            )
        return ScheduleApplyOutcome(
            result=result, plan=plan, draft=draft, created=created
        )

    async def _fingerprint_for_existing(
        self,
        user: UserAccount,
        draft_id: UUID,
        command: ApplyScheduleDraftCommand,
        existing_draft_id: UUID,
    ) -> str:
        if existing_draft_id != draft_id:
            raise ScheduleApplyIdempotencyConflict(
                "The client request ID was used for another Schedule Draft."
            )
        draft = await self._drafts.get_draft(user.id, draft_id)
        candidate = await self._drafts.get_candidate_set(user.id, draft_id)
        busy = await self._drafts.get_busy_snapshot(user.id, draft_id)
        source = await self._plans.get_revision_for_user(
            command.root_plan_id, user.id, command.source_revision
        )
        if draft is None or candidate is None or busy is None or source is None:
            raise ScheduleApplicationNotFound(
                "Schedule application inputs are missing."
            )
        return self._fingerprint(
            user_id=user.id,
            draft=draft,
            source=source,
            candidate=candidate,
            busy=busy,
            command=command,
        )

    @staticmethod
    def _fingerprint(
        *,
        user_id: UUID,
        draft: ScheduleDraft,
        source: WeeklyPlan,
        candidate: TimeSlotCandidateSet,
        busy: BusySnapshot,
        command: ApplyScheduleDraftCommand,
    ) -> str:
        payload = {
            "user_id": str(user_id),
            "draft_id": str(draft.id),
            "draft_version": command.expected_draft_version,
            "draft_fingerprint": draft.request_fingerprint,
            "root_plan_id": str(command.root_plan_id),
            "source_revision": command.source_revision,
            "expected_plan_version": command.expected_plan_version,
            "candidate_fingerprint": candidate.fingerprint,
            "busy_fingerprint": busy.fingerprint,
            "context_fingerprint": draft.context_fingerprint,
            "assignments": [
                {
                    "session_id": str(item.session_id),
                    "slot_id": item.slot_id,
                    "start": item.scheduled_start.astimezone(UTC).isoformat(),
                    "end": item.scheduled_end.astimezone(UTC).isoformat(),
                }
                for item in draft.assignments
            ],
            "source_plan": {
                "id": str(source.id),
                "version": source.version,
                "revision": source.revision,
                "sessions": [
                    {
                        "id": str(item.id),
                        "version": item.version,
                        "start": item.scheduled_start.isoformat(),
                        "end": item.scheduled_end.isoformat(),
                    }
                    for item in source.sessions
                ],
            },
            "policy_version": SchedulePlanMergePolicy.version,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
