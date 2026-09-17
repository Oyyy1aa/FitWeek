"""Apply an accepted Session Design Draft to one future Plan Session."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from app.application.errors import (
    ResourceNotFound,
    SessionDesignApplicationNotFound,
    SessionDesignApplyIdempotencyConflict,
    SessionDesignDraftAlreadyApplied,
    SessionDesignDraftExpired,
    SessionDesignDraftNotAccepted,
    SessionDesignDraftVersionConflict,
    SessionDesignPlanApplicationFailed,
    SessionDesignPlanSafetyFailed,
    SessionDesignPlanVersionConflict,
    SessionDesignSourceRevisionNotCurrent,
    SessionDesignTargetDurationMismatch,
    SessionDesignTargetLocationMismatch,
    SessionDesignTargetPlanNotFound,
    SessionDesignTargetSessionCheckedIn,
    SessionDesignTargetSessionImmutable,
    SessionDesignTargetSessionNotFound,
    SessionDesignTargetTypeMismatch,
)
from app.domain.checkins.models import WorkoutCheckIn
from app.domain.checkins.repositories import CheckInRepository
from app.domain.common import (
    RepositoryConflictError,
    RepositoryUniqueError,
    utc_now,
)
from app.domain.context.repositories import ContextSnapshotRepository
from app.domain.exercises.models import Exercise
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.plans.models import WeeklyPlan, WeeklyPlanStatus
from app.domain.plans.repositories import PlanRepository
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.domain.profiles.repositories import ProfileRepository
from app.domain.session_design.enums import SessionDesignDraftStatus
from app.domain.session_design.models import ExerciseCandidateSet, SessionDesignDraft
from app.domain.session_design.repositories import SessionDesignRepository
from app.domain.session_design_application.models import (
    APPLICATION_POLICY_VERSION,
    ApplySessionDesignCommand,
    SessionDesignApplicationResult,
    SessionDesignApplyPreview,
)
from app.domain.session_design_application.repositories import (
    SessionDesignApplicationRepository,
)
from app.domain.sessions.models import WorkoutSession, WorkoutSessionStatus
from app.domain.users.models import UserAccount
from app.safety.engine import SafetyEngine
from app.session_design_application.merge_policy import SessionDesignPlanMergePolicy


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionDesignApplyOutcome:
    result: SessionDesignApplicationResult
    plan: WeeklyPlan
    draft: SessionDesignDraft
    created: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class _PreparedApplication:
    source: WeeklyPlan
    draft: SessionDesignDraft
    candidate_set: ExerciseCandidateSet
    target: WorkoutSession
    check_ins: tuple[WorkoutCheckIn, ...]
    revision: WeeklyPlan
    preview: SessionDesignApplyPreview


class SessionDesignPlanApplicationService:
    """Deterministic read/merge/validate/atomic-commit application boundary."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        exercises: ExerciseRepository,
        plans: PlanRepository,
        check_ins: CheckInRepository,
        drafts: SessionDesignRepository,
        contexts: ContextSnapshotRepository,
        applications: SessionDesignApplicationRepository,
        safety: SafetyEngine,
        merge_policy: SessionDesignPlanMergePolicy | None = None,
    ) -> None:
        self._profiles = profiles
        self._exercises = exercises
        self._plans = plans
        self._check_ins = check_ins
        self._drafts = drafts
        self._contexts = contexts
        self._applications = applications
        self._safety = safety
        self._merge = merge_policy or SessionDesignPlanMergePolicy()

    async def preview(
        self,
        user: UserAccount,
        draft_id: UUID,
        command: ApplySessionDesignCommand,
    ) -> SessionDesignApplyPreview:
        prepared = await self._prepare(user, draft_id, command)
        return prepared.preview

    async def apply(
        self,
        user: UserAccount,
        draft_id: UUID,
        command: ApplySessionDesignCommand,
    ) -> SessionDesignApplyOutcome:
        existing = await self._applications.get_result_by_request(
            user.id, command.client_request_id
        )
        if existing is not None:
            fingerprint = await self._fingerprint_for_existing(
                user, draft_id, command, existing
            )
            if existing.application_fingerprint != fingerprint:
                raise SessionDesignApplyIdempotencyConflict(
                    "The client request ID was already used with a different payload."
                )
            return await self._outcome_for_result(user, existing, created=False)

        prepared = await self._prepare(user, draft_id, command)
        now = utc_now()
        result_id = uuid5(
            NAMESPACE_URL,
            "fitweek:session-design-application:"
            f"{user.id}:{command.client_request_id}:"
            f"{prepared.preview.application_fingerprint}",
        )
        result = SessionDesignApplicationResult(
            id=result_id,
            user_id=user.id,
            client_request_id=command.client_request_id,
            application_fingerprint=prepared.preview.application_fingerprint,
            draft_id=prepared.draft.id,
            root_plan_id=prepared.source.series_id,
            source_revision=prepared.source.revision,
            created_revision=prepared.revision.revision,
            target_session_id=prepared.target.id,
            previous_plan_version=prepared.source.version,
            resulting_plan_version=prepared.revision.version,
            created_at=now,
        )
        applied_draft = prepared.draft.mark_applied(
            root_plan_id=prepared.source.series_id,
            revision=prepared.revision.revision,
            session_id=prepared.target.id,
            application_result_id=result.id,
            at=now,
        )
        try:
            committed = await self._applications.commit(
                source=prepared.source,
                expected_draft=prepared.draft,
                applied_draft=applied_draft,
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
                return await self._outcome_for_result(user, concurrent, created=False)
            if exc.constraint.endswith("user_request"):
                raise SessionDesignApplyIdempotencyConflict(
                    "The client request ID conflicts with an existing application."
                ) from exc
            if exc.constraint.endswith("draft"):
                raise SessionDesignDraftAlreadyApplied(
                    "The Session Design Draft was already applied."
                ) from exc
            if exc.constraint == "weekly_plan.user_week_revision":
                raise SessionDesignPlanVersionConflict(
                    "A pending Plan Revision already occupies the next revision."
                ) from exc
            raise SessionDesignPlanApplicationFailed(
                "The Session Design application could not be committed."
            ) from exc
        except RepositoryConflictError as exc:
            if exc.resource == "SessionDesignDraft":
                raise SessionDesignDraftVersionConflict(
                    "The Session Design Draft changed before commit."
                ) from exc
            raise SessionDesignPlanVersionConflict(
                "The current Plan Revision changed before commit."
            ) from exc
        except RuntimeError as exc:
            raise SessionDesignPlanApplicationFailed(
                "The Session Design application could not be committed."
            ) from exc
        return await self._outcome_for_result(
            user, committed.result, created=committed.created
        )

    async def get_result(
        self, user: UserAccount, draft_id: UUID
    ) -> SessionDesignApplyOutcome:
        result = await self._applications.get_result_by_draft(user.id, draft_id)
        if result is None:
            raise SessionDesignApplicationNotFound(
                "Session Design application result was not found."
            )
        return await self._outcome_for_result(user, result, created=False)

    async def verify_result(
        self, user: UserAccount, result_id: UUID
    ) -> SessionDesignApplyOutcome:
        result = await self._applications.get_result(user.id, result_id)
        if result is None:
            raise SessionDesignApplicationNotFound(
                "Session Design application result was not found."
            )
        outcome = await self._outcome_for_result(user, result, created=False)
        profile, constraints = await self._profile_context(user.id)
        catalog = await self._load_catalog(outcome.plan)
        validation = self._safety.validate_plan(
            profile=profile,
            constraints=constraints,
            plan=outcome.plan,
            exercise_catalog=catalog,
        )
        if not validation.passed:
            raise SessionDesignPlanSafetyFailed(
                "The stored Plan Revision failed the full Safety gate.",
                violations=validation.violations,
            )
        return outcome

    async def _prepare(
        self,
        user: UserAccount,
        draft_id: UUID,
        command: ApplySessionDesignCommand,
    ) -> _PreparedApplication:
        now = utc_now()
        draft = await self._load_draft(user.id, draft_id)
        self._validate_draft(draft, command, now)
        candidate_set, context_fingerprint = await self._frozen_references(
            user.id, draft
        )
        source = await self._load_source(user.id, command)
        target = next(
            (item for item in source.sessions if item.id == command.target_session_id),
            None,
        )
        if target is None:
            raise SessionDesignTargetSessionNotFound(
                "The target Session was not found in the source Revision."
            )
        check_ins = tuple(await self._check_ins.list_by_series(source.series_id))
        self._validate_target(target, draft, check_ins, now)
        fingerprint = self._fingerprint(
            user_id=user.id,
            draft=draft,
            candidate_set=candidate_set,
            context_fingerprint=context_fingerprint,
            source=source,
            command=command,
        )
        revision, metadata = self._merge.build(
            source=source,
            draft=draft,
            target=target,
            check_ins=check_ins,
            fingerprint=fingerprint,
            created_at=now,
        )
        profile, constraints = await self._profile_context(user.id)
        catalog = await self._load_catalog(revision)
        self._validate_candidate_membership(draft, candidate_set, catalog)
        validation = self._safety.validate_plan(
            profile=profile,
            constraints=constraints,
            plan=revision,
            exercise_catalog=catalog,
        )
        if not validation.passed:
            raise SessionDesignPlanSafetyFailed(
                "The complete Plan Revision failed the deterministic Safety gate.",
                violations=validation.violations,
            )
        before = self._merge.summarize(target)
        after_target = next(item for item in revision.sessions if item.id == target.id)
        after = self._merge.summarize(after_target)
        preview = SessionDesignApplyPreview(
            draft_id=draft.id,
            draft_version=draft.version,
            root_plan_id=source.series_id,
            source_revision=source.revision,
            source_plan_version=source.version,
            target_session_id=target.id,
            target_session_before=before,
            target_session_after=after,
            preserved_session_ids=metadata.preserved_session_ids,
            immutable_session_ids=metadata.immutable_session_ids,
            changed_session_ids=metadata.changed_session_ids,
            duration_delta_seconds=(
                after.calculated_seconds - before.calculated_seconds
            ),
            available_window_seconds=int(
                (target.scheduled_end - target.scheduled_start).total_seconds()
            ),
            validation=validation,
            application_fingerprint=fingerprint,
        )
        return _PreparedApplication(
            source=source,
            draft=draft,
            candidate_set=candidate_set,
            target=target,
            check_ins=check_ins,
            revision=revision,
            preview=preview,
        )

    async def _load_draft(self, user_id: UUID, draft_id: UUID) -> SessionDesignDraft:
        draft = await self._drafts.get_draft(user_id, draft_id)
        if draft is None:
            raise ResourceNotFound("Session Design Draft was not found.")
        return draft

    @staticmethod
    def _validate_draft(
        draft: SessionDesignDraft,
        command: ApplySessionDesignCommand,
        now: datetime,
    ) -> None:
        if draft.status is SessionDesignDraftStatus.APPLIED:
            raise SessionDesignDraftAlreadyApplied(
                "The Session Design Draft was already applied."
            )
        if draft.status is SessionDesignDraftStatus.EXPIRED or draft.expires_at <= now:
            raise SessionDesignDraftExpired("The Session Design Draft has expired.")
        if draft.version != command.expected_draft_version:
            raise SessionDesignDraftVersionConflict(
                "The expected Session Design Draft version is stale."
            )
        if draft.status is not SessionDesignDraftStatus.ACCEPTED:
            raise SessionDesignDraftNotAccepted(
                "Only an ACCEPTED Session Design Draft can be applied."
            )

    async def _frozen_references(
        self, user_id: UUID, draft: SessionDesignDraft
    ) -> tuple[ExerciseCandidateSet, str]:
        candidate_set = await self._drafts.get_candidate_set(
            user_id, draft.candidate_set_id
        )
        if (
            candidate_set is None
            or candidate_set.fingerprint != draft.candidate_set_fingerprint
        ):
            raise SessionDesignDraftNotAccepted(
                "The frozen Candidate Set reference is unavailable or changed."
            )
        snapshot = await self._contexts.get(
            user_id, draft.context_snapshot_reference_id
        )
        if (
            snapshot is None
            or snapshot.reference.context_fingerprint != draft.context_fingerprint
        ):
            raise SessionDesignDraftNotAccepted(
                "The frozen Context Snapshot reference is unavailable or changed."
            )
        return candidate_set, snapshot.reference.context_fingerprint

    async def _load_source(
        self, user_id: UUID, command: ApplySessionDesignCommand
    ) -> WeeklyPlan:
        source = await self._plans.get_revision_for_user(
            command.root_plan_id, user_id, command.source_revision
        )
        if source is None:
            raise SessionDesignTargetPlanNotFound(
                "The source Weekly Plan Revision was not found."
            )
        if source.status is not WeeklyPlanStatus.CONFIRMED:
            raise SessionDesignSourceRevisionNotCurrent(
                "The source Revision is not confirmed."
            )
        current = await self._plans.get_current_confirmed(command.root_plan_id, user_id)
        if current is None or current.id != source.id:
            raise SessionDesignSourceRevisionNotCurrent(
                "Only the current confirmed Revision can be used."
            )
        if source.version != command.expected_plan_version:
            raise SessionDesignPlanVersionConflict(
                "The expected Plan version is stale."
            )
        return source

    @staticmethod
    def _validate_target(
        target: WorkoutSession,
        draft: SessionDesignDraft,
        check_ins: tuple[WorkoutCheckIn, ...],
        now: datetime,
    ) -> None:
        if any(item.session_id == target.id for item in check_ins):
            raise SessionDesignTargetSessionCheckedIn(
                "A checked-in Session is immutable."
            )
        if target.status is not WorkoutSessionStatus.PLANNED:
            raise SessionDesignTargetSessionImmutable(
                "Only a future PLANNED Session can receive a Session Design."
            )
        if target.scheduled_start <= now:
            raise SessionDesignTargetSessionImmutable(
                "A started or past Session is immutable."
            )
        if target.scheduled_start.astimezone(UTC).date() != draft.target_date:
            raise SessionDesignTargetSessionImmutable(
                "The Draft target date does not match the target Session."
            )
        if target.location_type is not draft.location:
            raise SessionDesignTargetLocationMismatch(
                "The Draft location does not match the target Session."
            )
        if (
            target.session_type is not draft.session_type
            and target.session_type.value != "MIXED"
        ):
            raise SessionDesignTargetTypeMismatch(
                "The Draft Session type does not match the target Session."
            )
        window_seconds = int(
            (target.scheduled_end - target.scheduled_start).total_seconds()
        )
        if (
            draft.duration.total_seconds > window_seconds
            or draft.duration.total_seconds < 15 * 60
            or draft.target_duration_minutes > target.estimated_minutes
        ):
            raise SessionDesignTargetDurationMismatch(
                "The accepted Draft does not fit the target Session window."
            )

    async def _profile_context(
        self, user_id: UUID
    ) -> tuple[FitnessProfile, tuple[UserConstraint, ...]]:
        profile = await self._profiles.get_by_user_id(user_id)
        if profile is None:
            raise ResourceNotFound("Fitness Profile was not found.")
        constraints = tuple(await self._profiles.list_constraints(profile.id))
        return profile, constraints

    async def _load_catalog(self, plan: WeeklyPlan) -> dict[str, Exercise]:
        catalog = {item.id: item for item in await self._exercises.list_active()}
        ids = {
            item.exercise_id for session in plan.sessions for item in session.exercises
        }
        for exercise_id in sorted(ids):
            if exercise_id not in catalog:
                exercise = await self._exercises.get(exercise_id)
                if exercise is not None:
                    catalog[exercise_id] = exercise
        return catalog

    @staticmethod
    def _validate_candidate_membership(
        draft: SessionDesignDraft,
        candidate_set: ExerciseCandidateSet,
        catalog: dict[str, Exercise],
    ) -> None:
        allowed = {
            exercise_id
            for slot in candidate_set.slots
            for exercise_id in slot.exercise_ids
        }
        if any(
            item.exercise_id not in allowed or item.exercise_id not in catalog
            for item in draft.exercises
        ):
            raise SessionDesignDraftNotAccepted(
                "The Draft cannot be verified against its frozen Candidate Set."
            )

    async def _fingerprint_for_existing(
        self,
        user: UserAccount,
        draft_id: UUID,
        command: ApplySessionDesignCommand,
        existing: SessionDesignApplicationResult,
    ) -> str:
        draft = await self._load_draft(user.id, draft_id)
        candidate_set, context_fingerprint = await self._frozen_references(
            user.id, draft
        )
        source = await self._plans.get_revision_for_user(
            command.root_plan_id, user.id, command.source_revision
        )
        if source is None:
            raise SessionDesignTargetPlanNotFound(
                "The source Weekly Plan Revision was not found."
            )
        if existing.draft_id != draft_id:
            raise SessionDesignApplyIdempotencyConflict(
                "The client request ID was used for another Draft."
            )
        return self._fingerprint(
            user_id=user.id,
            draft=draft,
            candidate_set=candidate_set,
            context_fingerprint=context_fingerprint,
            source=source,
            command=command,
        )

    async def _outcome_for_result(
        self,
        user: UserAccount,
        result: SessionDesignApplicationResult,
        *,
        created: bool,
    ) -> SessionDesignApplyOutcome:
        plan = await self._plans.get_revision_for_user(
            result.root_plan_id, user.id, result.created_revision
        )
        draft = await self._drafts.get_draft(user.id, result.draft_id)
        if plan is None or draft is None:
            raise SessionDesignApplicationNotFound(
                "The Session Design application audit references are incomplete."
            )
        return SessionDesignApplyOutcome(
            result=result, plan=plan, draft=draft, created=created
        )

    @staticmethod
    def _fingerprint(
        *,
        user_id: UUID,
        draft: SessionDesignDraft,
        candidate_set: ExerciseCandidateSet,
        context_fingerprint: str,
        source: WeeklyPlan,
        command: ApplySessionDesignCommand,
    ) -> str:
        payload = {
            "user_id": str(user_id),
            "draft_id": str(draft.id),
            "draft_version": command.expected_draft_version,
            "draft_fingerprint": draft.request_payload_fingerprint,
            "candidate_fingerprint": candidate_set.fingerprint,
            "context_fingerprint": context_fingerprint,
            "root_plan_id": str(command.root_plan_id),
            "source_revision": command.source_revision,
            "expected_plan_version": command.expected_plan_version,
            "target_session_id": str(command.target_session_id),
            "source_plan_fingerprint": (
                SessionDesignPlanApplicationService._plan_fingerprint(source)
            ),
            "policy_version": APPLICATION_POLICY_VERSION,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _plan_fingerprint(plan: WeeklyPlan) -> str:
        payload = {
            "id": str(plan.id),
            "series": str(plan.series_id),
            "revision": plan.revision,
            "version": plan.version,
            "status": plan.status.value,
            "sessions": [
                {
                    "id": str(session.id),
                    "version": session.version,
                    "start": session.scheduled_start.isoformat(),
                    "end": session.scheduled_end.isoformat(),
                    "location": session.location_type.value,
                    "type": session.session_type.value,
                    "exercises": [
                        {
                            "id": item.exercise_id,
                            "sequence": item.sequence_no,
                            "sets": item.sets,
                            "repetitions": item.repetitions,
                            "duration": item.duration_seconds,
                            "rest": item.rest_seconds,
                        }
                        for item in session.exercises
                    ],
                }
                for session in plan.sessions
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
