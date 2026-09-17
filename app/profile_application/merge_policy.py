"""Side-effect-free Profile Draft selection and merge policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from uuid import UUID, uuid5

from app.domain.profile_agent.apply_models import (
    APPLY_POLICY_VERSION,
    ApplyValidationCode,
    ApplyValidationIssue,
    ApplyValidationResult,
    ConstraintConflict,
    ConstraintPreview,
    FitnessProfilePreview,
    ProfileDraftApplyDecision,
    ProfileDraftApplyPreview,
    ProfileDraftMergeOutcome,
    ProfileFieldChange,
)
from app.domain.profile_agent.models import ProfileAgentDraft
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
    FitnessProfile,
    UserConstraint,
)
from app.domain.profiles.normalization import normalize_constraint_value
from app.profile_application.validation import ProfileDraftMergeError

_PROFILE_ID_NAMESPACE = UUID("27fd48d4-0696-4e53-bdce-43f4aa94da9c")
_CONSTRAINT_ID_NAMESPACE = UUID("7cdbbbd8-5448-4a98-912e-e40e00e02f77")
_SINGLE_VALUE_CONSTRAINTS = frozenset({ConstraintType.MAX_SESSION_MINUTES})


def build_apply_fingerprint(
    *,
    user_id: UUID,
    draft_id: UUID,
    decision: ProfileDraftApplyDecision,
) -> str:
    canonical = json.dumps(
        {
            "user_id": str(user_id),
            "draft_id": str(draft_id),
            "expected_draft_version": decision.expected_draft_version,
            "expected_profile_version": decision.expected_profile_version,
            "accept_weekly_frequency": decision.accept_weekly_frequency,
            "accept_max_session_minutes": decision.accept_max_session_minutes,
            "selected_primary_goal": (
                decision.selected_primary_goal.value
                if decision.selected_primary_goal is not None
                else None
            ),
            "accepted_equipment": sorted(decision.accepted_equipment),
            "accepted_locations": sorted(
                item.value for item in decision.accepted_locations
            ),
            "accepted_hard_constraint_indexes": sorted(
                decision.accepted_hard_constraint_indexes
            ),
            "accepted_temporary_constraint_indexes": sorted(
                decision.accepted_temporary_constraint_indexes
            ),
            "temporary_constraint_expirations": {
                str(index): value.isoformat()
                for index, value in sorted(
                    decision.temporary_constraint_expirations.items()
                )
            },
            "confirmed_experience_level": (
                decision.confirmed_experience_level.value
                if decision.confirmed_experience_level is not None
                else None
            ),
            "confirm_scope": decision.confirm_scope,
            "policy_version": APPLY_POLICY_VERSION,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProfileDraftMergePolicy:
    """Build exactly one preview and immutable commit candidate set."""

    def merge(
        self,
        *,
        user_id: UUID,
        draft: ProfileAgentDraft,
        current_profile: FitnessProfile | None,
        existing_constraints: tuple[UserConstraint, ...],
        decision: ProfileDraftApplyDecision,
        now: datetime,
    ) -> ProfileDraftMergeOutcome:
        output = draft.output
        fingerprint = build_apply_fingerprint(
            user_id=user_id,
            draft_id=draft.id,
            decision=decision,
        )
        self._validate_selections(draft, decision, now)
        profile = self._profile_candidate(
            user_id=user_id,
            draft=draft,
            current=current_profile,
            decision=decision,
            now=now,
        )
        changes = self._profile_changes(current_profile, profile)
        proposed = self._constraint_candidates(
            draft=draft,
            profile_id=profile.id,
            decision=decision,
            now=now,
        )
        to_add, unchanged, conflicts = self._classify_constraints(
            proposed=proposed,
            existing=existing_constraints,
        )
        issues = tuple(
            ApplyValidationIssue(
                code=ApplyValidationCode.CONSTRAINT_CONFLICT,
                message=item.message,
                path="constraints",
            )
            for item in conflicts
        )
        preview = ProfileDraftApplyPreview(
            draft_id=draft.id,
            draft_version=draft.version,
            expires_at=draft.expires_at,
            context_snapshot_reference_id=draft.context_snapshot_reference_id,
            context_fingerprint=draft.context_fingerprint,
            context_contract_version=draft.context_contract_version,
            context_policy_version=draft.context_policy_version,
            context_degraded_mode=draft.context_degraded_mode,
            current_profile_version=(
                current_profile.version if current_profile is not None else None
            ),
            profile_changes=changes,
            constraints_to_add=tuple(
                self._constraint_preview(item, "ADD") for item in to_add
            ),
            constraints_unchanged=unchanged,
            constraints_conflicting=conflicts,
            ignored_soft_preferences=output.soft_preferences,
            ignored_memory_candidates=output.memory_candidates,
            result_profile=FitnessProfilePreview(
                profile_id=profile.id,
                experience_level=profile.experience_level,
                weekly_frequency=profile.weekly_frequency,
                max_session_minutes=profile.max_session_minutes,
                primary_goal=profile.primary_goal,
                scope_confirmed=profile.scope_confirmed,
                resulting_version=profile.version,
            ),
            validation=ApplyValidationResult(passed=not issues, issues=issues),
            apply_fingerprint=fingerprint,
        )
        return ProfileDraftMergeOutcome(
            preview=preview,
            profile=profile,
            constraints_to_add=to_add,
            expected_constraint_versions=tuple(
                sorted((item.id, item.version) for item in existing_constraints)
            ),
        )

    @staticmethod
    def _validate_selections(
        draft: ProfileAgentDraft,
        decision: ProfileDraftApplyDecision,
        now: datetime,
    ) -> None:
        output = draft.output
        if decision.accept_weekly_frequency and output.weekly_frequency is None:
            raise ProfileDraftMergeError(
                ApplyValidationCode.INVALID_DRAFT_SELECTION,
                "The Draft has no weekly frequency to accept.",
            )
        if decision.accept_max_session_minutes and output.max_session_minutes is None:
            raise ProfileDraftMergeError(
                ApplyValidationCode.INVALID_DRAFT_SELECTION,
                "The Draft has no session duration to accept.",
            )
        if (
            decision.selected_primary_goal is not None
            and decision.selected_primary_goal not in output.goals
        ):
            raise ProfileDraftMergeError(
                ApplyValidationCode.INVALID_DRAFT_SELECTION,
                "The selected goal is not present in the Draft.",
            )
        if len(output.goals) > 1 and decision.selected_primary_goal is None:
            raise ProfileDraftMergeError(
                ApplyValidationCode.PRIMARY_GOAL_SELECTION_REQUIRED,
                "A Draft with multiple goals requires an explicit primary goal.",
            )
        if not set(decision.accepted_equipment).issubset(output.equipment):
            raise ProfileDraftMergeError(
                ApplyValidationCode.INVALID_DRAFT_SELECTION,
                "Accepted equipment must come from the Draft.",
            )
        if not set(decision.accepted_locations).issubset(output.locations):
            raise ProfileDraftMergeError(
                ApplyValidationCode.INVALID_DRAFT_SELECTION,
                "Accepted locations must come from the Draft.",
            )
        for indexes, size, label in (
            (
                decision.accepted_hard_constraint_indexes,
                len(output.hard_constraints),
                "hard constraint",
            ),
            (
                decision.accepted_temporary_constraint_indexes,
                len(output.temporary_constraints),
                "temporary constraint",
            ),
        ):
            if any(index < 0 or index >= size for index in indexes):
                raise ProfileDraftMergeError(
                    ApplyValidationCode.INVALID_DRAFT_SELECTION,
                    f"Accepted {label} index is outside the Draft.",
                )
        accepted_temporary = set(decision.accepted_temporary_constraint_indexes)
        if set(decision.temporary_constraint_expirations) != accepted_temporary:
            raise ProfileDraftMergeError(
                ApplyValidationCode.TEMPORARY_CONSTRAINT_EXPIRATION_REQUIRED,
                "Every accepted temporary constraint requires exactly one expiration.",
            )
        if any(
            expiration <= now
            for expiration in decision.temporary_constraint_expirations.values()
        ):
            raise ProfileDraftMergeError(
                ApplyValidationCode.TEMPORARY_CONSTRAINT_EXPIRATION_REQUIRED,
                "Temporary constraint expiration must be in the future.",
            )

    @staticmethod
    def _profile_candidate(
        *,
        user_id: UUID,
        draft: ProfileAgentDraft,
        current: FitnessProfile | None,
        decision: ProfileDraftApplyDecision,
        now: datetime,
    ) -> FitnessProfile:
        output = draft.output
        if current is None:
            if decision.confirmed_experience_level is None:
                raise ProfileDraftMergeError(
                    ApplyValidationCode.EXPERIENCE_LEVEL_REQUIRED,
                    "Creating a profile requires an explicit experience level.",
                )
            if not decision.confirm_scope:
                raise ProfileDraftMergeError(
                    ApplyValidationCode.SCOPE_CONFIRMATION_REQUIRED,
                    "Creating a profile requires explicit scope confirmation.",
                )
            if not decision.accept_weekly_frequency or output.weekly_frequency is None:
                raise ProfileDraftMergeError(
                    ApplyValidationCode.INVALID_DRAFT_SELECTION,
                    "Creating a profile requires an accepted weekly frequency.",
                )
            if (
                not decision.accept_max_session_minutes
                or output.max_session_minutes is None
            ):
                raise ProfileDraftMergeError(
                    ApplyValidationCode.INVALID_DRAFT_SELECTION,
                    "Creating a profile requires an accepted session duration.",
                )
            if decision.selected_primary_goal is None:
                raise ProfileDraftMergeError(
                    ApplyValidationCode.PRIMARY_GOAL_SELECTION_REQUIRED,
                    "Creating a profile requires an explicit primary goal.",
                )
            return FitnessProfile(
                id=uuid5(_PROFILE_ID_NAMESPACE, str(user_id)),
                user_id=user_id,
                experience_level=decision.confirmed_experience_level,
                weekly_frequency=output.weekly_frequency,
                max_session_minutes=output.max_session_minutes,
                primary_goal=decision.selected_primary_goal,
                scope_confirmed=True,
                created_at=now,
                updated_at=now,
                version=1,
            )
        scope_confirmed = current.scope_confirmed or decision.confirm_scope
        if not scope_confirmed:
            raise ProfileDraftMergeError(
                ApplyValidationCode.SCOPE_CONFIRMATION_REQUIRED,
                "The resulting profile must have explicit scope confirmation.",
            )
        weekly_frequency = current.weekly_frequency
        if decision.accept_weekly_frequency:
            if output.weekly_frequency is None:
                raise ProfileDraftMergeError(
                    ApplyValidationCode.INVALID_DRAFT_SELECTION,
                    "The Draft has no weekly frequency to accept.",
                )
            weekly_frequency = output.weekly_frequency
        max_session_minutes = current.max_session_minutes
        if decision.accept_max_session_minutes:
            if output.max_session_minutes is None:
                raise ProfileDraftMergeError(
                    ApplyValidationCode.INVALID_DRAFT_SELECTION,
                    "The Draft has no session duration to accept.",
                )
            max_session_minutes = output.max_session_minutes
        return replace(
            current,
            weekly_frequency=weekly_frequency,
            max_session_minutes=max_session_minutes,
            primary_goal=decision.selected_primary_goal or current.primary_goal,
            scope_confirmed=scope_confirmed,
            updated_at=now,
            version=current.version + 1,
        )

    @staticmethod
    def _profile_changes(
        current: FitnessProfile | None,
        result: FitnessProfile,
    ) -> tuple[ProfileFieldChange, ...]:
        changes: list[ProfileFieldChange] = []
        fields = (
            "experience_level",
            "weekly_frequency",
            "max_session_minutes",
            "primary_goal",
            "scope_confirmed",
        )
        for field in fields:
            old = getattr(current, field) if current is not None else None
            new = getattr(result, field)
            old_value = ProfileDraftMergePolicy._profile_value(old)
            new_value = ProfileDraftMergePolicy._profile_value(new)
            if old_value != new_value:
                changes.append(
                    ProfileFieldChange(
                        field_name=field,
                        old_value=old_value,
                        new_value=new_value,
                    )
                )
        return tuple(changes)

    @staticmethod
    def _profile_value(value: object) -> str | int | bool | None:
        if isinstance(value, (ExperienceLevel, FitnessGoal)):
            return value.value
        if value is None or isinstance(value, (str, int, bool)):
            return value
        raise TypeError("Profile preview value is unsupported.")

    @staticmethod
    def _constraint_candidates(
        *,
        draft: ProfileAgentDraft,
        profile_id: UUID,
        decision: ProfileDraftApplyDecision,
        now: datetime,
    ) -> tuple[UserConstraint, ...]:
        specs: list[tuple[ConstraintType, str, int, bool, datetime | None]] = []
        specs.extend(
            (ConstraintType.AVAILABLE_EQUIPMENT, value, 100, True, None)
            for value in decision.accepted_equipment
        )
        specs.extend(
            (ConstraintType.ALLOWED_LOCATION, value.value, 100, True, None)
            for value in decision.accepted_locations
        )
        for index in decision.accepted_hard_constraint_indexes:
            proposal = draft.output.hard_constraints[index]
            if not proposal.is_hard:
                raise ProfileDraftMergeError(
                    ApplyValidationCode.INVALID_DRAFT_SELECTION,
                    "A hard-constraint selection must reference a hard proposal.",
                )
            specs.append(
                (
                    proposal.constraint_type,
                    proposal.value,
                    proposal.priority,
                    True,
                    None,
                )
            )
        for index in decision.accepted_temporary_constraint_indexes:
            proposal = draft.output.temporary_constraints[index]
            specs.append(
                (
                    proposal.constraint_type,
                    proposal.value,
                    proposal.priority,
                    proposal.is_hard,
                    decision.temporary_constraint_expirations[index],
                )
            )
        candidates: list[UserConstraint] = []
        for constraint_type, raw, priority, is_hard, valid_until in specs:
            try:
                normalized = normalize_constraint_value(constraint_type, raw)
            except (TypeError, ValueError) as exc:
                raise ProfileDraftMergeError(
                    ApplyValidationCode.INVALID_DRAFT_SELECTION,
                    "A selected constraint has an invalid controlled value.",
                ) from exc
            identity = (
                f"{draft.id}:{constraint_type.value}:{normalized}:"
                f"{is_hard}:{valid_until.isoformat() if valid_until else 'permanent'}"
            )
            candidates.append(
                UserConstraint(
                    id=uuid5(_CONSTRAINT_ID_NAMESPACE, identity),
                    profile_id=profile_id,
                    constraint_type=constraint_type,
                    constraint_value=normalized,
                    priority=priority,
                    is_hard=is_hard,
                    source=ConstraintSource.USER_CONFIRMED_AGENT_DRAFT,
                    valid_until=valid_until,
                    created_at=now,
                    version=1,
                )
            )
        unique: dict[tuple[ConstraintType, str, bool], UserConstraint] = {}
        for candidate in candidates:
            unique.setdefault(
                (
                    candidate.constraint_type,
                    candidate.constraint_value,
                    candidate.is_hard,
                ),
                candidate,
            )
        return tuple(
            sorted(
                unique.values(),
                key=lambda item: (
                    item.constraint_type.value,
                    item.constraint_value,
                    item.id,
                ),
            )
        )

    def _classify_constraints(
        self,
        *,
        proposed: tuple[UserConstraint, ...],
        existing: tuple[UserConstraint, ...],
    ) -> tuple[
        tuple[UserConstraint, ...],
        tuple[ConstraintPreview, ...],
        tuple[ConstraintConflict, ...],
    ]:
        to_add: list[UserConstraint] = []
        unchanged: list[ConstraintPreview] = []
        conflicts: list[ConstraintConflict] = []
        for item in proposed:
            duplicate = next(
                (
                    current
                    for current in existing
                    if current.constraint_type is item.constraint_type
                    and current.constraint_value == item.constraint_value
                    and current.is_hard is item.is_hard
                ),
                None,
            )
            if duplicate is not None:
                unchanged.append(self._constraint_preview(duplicate, "UNCHANGED"))
                continue
            conflict = next(
                (
                    current
                    for current in existing
                    if item.constraint_type in _SINGLE_VALUE_CONSTRAINTS
                    and current.constraint_type is item.constraint_type
                    and current.is_hard
                    and item.is_hard
                    and current.constraint_value != item.constraint_value
                ),
                None,
            )
            if conflict is not None:
                conflicts.append(
                    ConstraintConflict(
                        constraint_type=item.constraint_type,
                        existing_value=conflict.constraint_value,
                        proposed_value=item.constraint_value,
                        message=(
                            "A hard single-value constraint already has another value."
                        ),
                    )
                )
                continue
            to_add.append(item)
        return tuple(to_add), tuple(unchanged), tuple(conflicts)

    @staticmethod
    def _constraint_preview(
        item: UserConstraint,
        reason: str,
    ) -> ConstraintPreview:
        return ConstraintPreview(
            constraint_id=item.id,
            constraint_type=item.constraint_type,
            constraint_value=item.constraint_value,
            priority=item.priority,
            is_hard=item.is_hard,
            valid_until=item.valid_until,
            reason=reason,
        )
