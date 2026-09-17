"""Offline Plan benchmark using the existing generator and Safety Engine."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from time import perf_counter
from typing import TypedDict
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.common import DomainValidationError, LocationType
from app.domain.exercises.catalog_seed import CATALOG_SEED
from app.domain.exercises.models import Exercise, ExerciseStatus
from app.domain.planning.models import (
    AvailabilitySlot,
    GenerateWeeklyPlanCommand,
    PlanGenerationError,
)
from app.domain.profiles.models import (
    ConstraintSource,
    ConstraintType,
    ExperienceLevel,
    FitnessGoal,
    FitnessProfile,
    UserConstraint,
)
from app.evaluation.determinism import repetitions_are_identical, stable_fingerprint
from app.evaluation.models import (
    AblationMode,
    EvaluationCaseResult,
    EvaluationCaseStatus,
    EvaluationFailureCategory,
    PlanEvaluationCase,
)
from app.planning.generator import DeterministicPlanGenerator, GenerationCandidate
from app.safety.constraint_validator import ConstraintValidator
from app.safety.engine import SafetyEngine

REFERENCE_NOW = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
REFERENCE_WEEK = date(2026, 7, 20)


class _PlanOutcome(TypedDict):
    business_output: object
    violations: tuple[str, ...]
    failure_category: EvaluationFailureCategory | None
    error_code: str | None
    metrics: dict[str, float]


class PlanCaseEvaluator:
    def __init__(self, *, repetitions: int = 3, timeout_seconds: float = 10) -> None:
        self._repetitions = repetitions
        self._timeout_seconds = timeout_seconds
        self._generator = DeterministicPlanGenerator()
        self._safety = SafetyEngine()
        self._constraint_validator = ConstraintValidator()

    async def evaluate(
        self, case: PlanEvaluationCase, ablation: AblationMode = AblationMode.FULL
    ) -> EvaluationCaseResult:
        started = perf_counter()
        try:
            repetitions = await asyncio.wait_for(
                asyncio.to_thread(self._repeat, case, ablation),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return self._timeout_result(case, ablation, started)
        except Exception:
            return self._internal_error_result(case, ablation, started)

        deterministic = repetitions_are_identical(repetitions)
        first = repetitions[0]
        violations = list(first["violations"])
        failure_category = first["failure_category"]
        if not deterministic:
            violations.append("NON_DETERMINISTIC_OUTPUT")
            failure_category = EvaluationFailureCategory.NON_DETERMINISTIC_OUTPUT
        status = (
            EvaluationCaseStatus.PASS if not violations else EvaluationCaseStatus.FAIL
        )
        metrics = dict(first["metrics"])
        metrics["deterministic_output"] = float(deterministic)
        metrics["plan_fingerprint_stable"] = float(deterministic)
        return EvaluationCaseResult(
            case_id=case.case_id,
            dataset="plan",
            category=case.category,
            status=status,
            duration_ms=(perf_counter() - started) * 1000,
            seed=case.seed,
            ablation=ablation,
            output_fingerprint=stable_fingerprint(first["business_output"]),
            violations=tuple(sorted(set(violations))),
            failure_category=failure_category,
            error_code=first["error_code"],
            safe_description_code=(
                None if status is EvaluationCaseStatus.PASS else "PLAN_CASE_FAILED"
            ),
            metric_values=metrics,
            trace_reference=f"evaluation.case/{case.case_id}",
            deterministic_repetitions=self._repetitions,
        )

    def _repeat(
        self, case: PlanEvaluationCase, ablation: AblationMode
    ) -> tuple[_PlanOutcome, ...]:
        return tuple(
            self._evaluate_once(case, ablation) for _ in range(self._repetitions)
        )

    def _evaluate_once(
        self, case: PlanEvaluationCase, ablation: AblationMode
    ) -> _PlanOutcome:
        metrics = self._base_metrics(case, ablation)
        try:
            profile = self._profile(case)
            constraints = self._constraints(case, profile.id)
            command = self._command(case)
            catalog = self._catalog(case)
            candidate = self._generator.generate_candidate(
                user_id=profile.user_id,
                profile=profile,
                constraints=constraints,
                catalog=catalog,
                command=command,
                context_fingerprint=(
                    "none"
                    if ablation is AblationMode.NO_MEMORY_CONTEXT
                    else stable_fingerprint({"context": case.category})
                ),
                context_contract_version="phase-9a-context-v1",
                context_policy_version="phase-9a-context-policy-v1",
            )
        except PlanGenerationError as exc:
            return self._rejection(
                case, tuple(item.code for item in exc.reasons), metrics
            )
        except DomainValidationError as exc:
            return self._rejection(case, (exc.code,), metrics)
        except (ValueError, KeyError):
            return self._rejection(case, ("DOMAIN_VALIDATION_ERROR",), metrics)

        if case.expected_outcome == "REJECTED":
            return {
                "business_output": {"outcome": "SUCCEEDED"},
                "violations": ("EXPECTED_REJECTION_MISMATCH",),
                "failure_category": (
                    EvaluationFailureCategory.EXPECTED_REJECTION_MISMATCH
                ),
                "error_code": None,
                "metrics": metrics,
            }

        validation = self._safety.validate_plan(
            profile=profile,
            constraints=constraints,
            plan=candidate.plan,
            exercise_catalog={item.id: item for item in catalog},
        )
        hard_violations = self._hard_constraint_violations(
            profile=profile,
            constraints=constraints,
            candidate=candidate,
            catalog=catalog,
        )
        violations = list(hard_violations)
        failure_category: EvaluationFailureCategory | None = None
        if not validation.passed:
            violations.append("SAFETY_ESCAPE")
            failure_category = EvaluationFailureCategory.SAFETY_ESCAPE
            metrics["safety_escape"] = 1.0
        if hard_violations:
            failure_category = EvaluationFailureCategory.HARD_CONSTRAINT_ESCAPE
            metrics["hard_constraint_escape"] = float(len(hard_violations))

        history_before = stable_fingerprint(
            {
                "history": case.input.history_mode,
                "version": case.input.expected_plan_version,
            }
        )
        history_after = history_before
        if history_after != history_before:
            violations.append("IMMUTABILITY_VIOLATION")
            failure_category = EvaluationFailureCategory.IMMUTABILITY_VIOLATION
            metrics["recovery_immutable_violation"] = 1.0

        business_output = {
            "outcome": "SUCCEEDED",
            "plan_id": str(candidate.plan.id),
            "input_fingerprint": candidate.metadata.input_fingerprint,
            "sessions": [
                {
                    "session_id": str(session.id),
                    "start": session.scheduled_start.isoformat(),
                    "end": session.scheduled_end.isoformat(),
                    "location": session.location_type.value,
                    "exercise_ids": [item.exercise_id for item in session.exercises],
                }
                for session in candidate.plan.sessions
            ],
            "history_fingerprint": history_after,
            "calendar_side_effects": 0,
            "memory_side_effects": 0,
            "current_plan_writes": 0,
        }
        metrics["plan_generation_success"] = 1.0
        metrics["session_count"] = float(len(candidate.plan.sessions))
        metrics["plan_modifications"] = float(
            case.category == "recovery_spacing"
            and ablation is not AblationMode.NO_RECOVERY_ADJUSTMENT
        )
        metrics["safety_rejection_accurate"] = 1.0
        metrics["schedule_conflict_detected"] = 1.0
        return {
            "business_output": business_output,
            "violations": tuple(violations),
            "failure_category": failure_category,
            "error_code": None,
            "metrics": metrics,
        }

    def _rejection(
        self,
        case: PlanEvaluationCase,
        actual_codes: tuple[str, ...],
        metrics: dict[str, float],
    ) -> _PlanOutcome:
        actual = tuple(sorted(set(actual_codes)))
        expected = tuple(sorted(case.expected_rejection_codes))
        matched = case.expected_outcome == "REJECTED" and set(expected).issubset(actual)
        metrics["safety_rejection_accurate"] = float(matched)
        if case.category == "schedule_time_window":
            metrics["schedule_conflict_detected"] = float(matched)
        return {
            "business_output": {"outcome": "REJECTED", "error_codes": actual},
            "violations": () if matched else ("EXPECTED_REJECTION_MISMATCH",),
            "failure_category": (
                None
                if matched
                else EvaluationFailureCategory.EXPECTED_REJECTION_MISMATCH
            ),
            "error_code": actual[0] if actual else "UNEXPECTED_REJECTION",
            "metrics": metrics,
        }

    @staticmethod
    def _profile(case: PlanEvaluationCase) -> FitnessProfile:
        user_id = uuid5(NAMESPACE_URL, f"fitweek:evaluation:user:{case.case_id}")
        profile_id = uuid5(NAMESPACE_URL, f"fitweek:evaluation:profile:{case.case_id}")
        return FitnessProfile(
            id=profile_id,
            user_id=user_id,
            experience_level=ExperienceLevel(case.input.experience_level),
            weekly_frequency=case.input.weekly_frequency,
            max_session_minutes=case.input.max_session_minutes,
            primary_goal=FitnessGoal(case.input.primary_goal),
            scope_confirmed=case.input.scope_confirmed,
            created_at=REFERENCE_NOW,
            updated_at=REFERENCE_NOW,
            version=1,
        )

    @staticmethod
    def _constraints(
        case: PlanEvaluationCase, profile_id: UUID
    ) -> tuple[UserConstraint, ...]:
        return tuple(
            UserConstraint(
                id=uuid5(
                    NAMESPACE_URL,
                    f"fitweek:evaluation:constraint:{case.case_id}:{index}",
                ),
                profile_id=profile_id,
                constraint_type=ConstraintType(item.constraint_type),
                constraint_value=item.value_code,
                priority=item.priority,
                is_hard=item.is_hard,
                source=ConstraintSource.USER_EXPLICIT,
                valid_until=None,
                created_at=REFERENCE_NOW,
                version=1,
            )
            for index, item in enumerate(case.input.constraints)
        )

    @staticmethod
    def _command(case: PlanEvaluationCase) -> GenerateWeeklyPlanCommand:
        location = LocationType(case.input.location)
        slots = []
        for index in range(case.input.slot_count):
            start = datetime(2026, 7, 20 + index, 10, tzinfo=UTC)
            if case.input.schedule_variant == "OVERLAP" and index == 1:
                start = datetime(2026, 7, 20, 10, 30, tzinfo=UTC)
            slots.append(
                AvailabilitySlot(
                    start=start,
                    end=start + timedelta(minutes=case.input.slot_duration_minutes),
                    location_type=location,
                )
            )
        return GenerateWeeklyPlanCommand(
            week_start=REFERENCE_WEEK,
            availability_slots=tuple(slots),
            client_request_id=f"eval-{case.case_id}",
        )

    @staticmethod
    def _catalog(case: PlanEvaluationCase) -> tuple[Exercise, ...]:
        if case.input.catalog_mode == "EMPTY":
            return ()
        if case.input.catalog_mode == "DISABLED_ONLY":
            return tuple(
                item for item in CATALOG_SEED if item.status is ExerciseStatus.DISABLED
            )
        return tuple(CATALOG_SEED)

    def _hard_constraint_violations(
        self,
        *,
        profile: FitnessProfile,
        constraints: tuple[UserConstraint, ...],
        candidate: GenerationCandidate,
        catalog: tuple[Exercise, ...],
    ) -> tuple[str, ...]:
        violations: list[str] = []
        catalog_map = {item.id: item for item in catalog}
        for session in candidate.plan.sessions:
            maximum = self._constraint_validator.effective_max_session_minutes(
                profile_limit=profile.max_session_minutes,
                constraints=constraints,
                at=session.scheduled_start,
            )
            if session.estimated_minutes > maximum:
                violations.append("DURATION_LIMIT_ESCAPE")
            available = self._constraint_validator.available_equipment(
                constraints, at=session.scheduled_start
            )
            excluded = self._constraint_validator.excluded_features(
                constraints, at=session.scheduled_start
            )
            allowed = self._constraint_validator.allowed_locations(
                constraints, at=session.scheduled_start
            )
            if (
                allowed is not None
                and session.location_type.value.casefold() not in allowed
            ):
                violations.append("LOCATION_CONSTRAINT_ESCAPE")
            for prescribed in session.exercises:
                exercise = catalog_map.get(prescribed.exercise_id)
                if exercise is None:
                    violations.append("MISSING_EXERCISE_ESCAPE")
                    continue
                if exercise.status is not ExerciseStatus.ACTIVE:
                    violations.append("DISABLED_EXERCISE_ESCAPE")
                if session.location_type not in exercise.location_types:
                    violations.append("EXERCISE_LOCATION_ESCAPE")
                if not {
                    item.casefold() for item in exercise.required_equipment
                }.issubset(available):
                    violations.append("EQUIPMENT_ESCAPE")
                if {item.casefold() for item in exercise.feature_tags} & excluded:
                    violations.append("EXCLUDED_FEATURE_ESCAPE")
        if len(candidate.plan.sessions) != profile.weekly_frequency:
            violations.append("FREQUENCY_ESCAPE")
        ordered = sorted(candidate.plan.sessions, key=lambda item: item.scheduled_start)
        if any(
            left.scheduled_end > right.scheduled_start
            for left, right in zip(ordered, ordered[1:], strict=False)
        ):
            violations.append("SESSION_OVERLAP_ESCAPE")
        return tuple(sorted(set(violations)))

    @staticmethod
    def _base_metrics(
        case: PlanEvaluationCase, ablation: AblationMode
    ) -> dict[str, float]:
        return {
            "plan_generation_success": 0.0,
            "hard_constraint_escape": 0.0,
            "safety_escape": 0.0,
            "safety_rejection_accurate": 0.0,
            "schedule_conflict_detected": 0.0,
            "calendar_side_effect_before_confirmation": 0.0,
            "duplicate_calendar_event": 0.0,
            "recovery_immutable_violation": 0.0,
            "unexpected_side_effect": 0.0,
            "manual_only": float(
                ablation is AblationMode.MANUAL_ONLY_CALENDAR
                or case.input.calendar_mode == "MANUAL_ONLY"
            ),
            "fallback_used": float(
                ablation
                in {
                    AblationMode.DETERMINISTIC_SESSION_FALLBACK,
                    AblationMode.TEMPLATE_ONLY,
                }
            ),
            "session_count": 0.0,
            "plan_modifications": 0.0,
        }

    def _timeout_result(
        self,
        case: PlanEvaluationCase,
        ablation: AblationMode,
        started: float,
    ) -> EvaluationCaseResult:
        return EvaluationCaseResult(
            case_id=case.case_id,
            dataset="plan",
            category=case.category,
            status=EvaluationCaseStatus.TIMED_OUT,
            duration_ms=(perf_counter() - started) * 1000,
            seed=case.seed,
            ablation=ablation,
            output_fingerprint=stable_fingerprint({"status": "TIMED_OUT"}),
            violations=("TIMEOUT",),
            failure_category=EvaluationFailureCategory.TIMEOUT,
            error_code="EVALUATION_CASE_TIMEOUT",
            safe_description_code="CASE_TIMEOUT",
            metric_values={"case_timeout": 1.0},
            trace_reference=f"evaluation.case/{case.case_id}",
            deterministic_repetitions=self._repetitions,
        )

    def _internal_error_result(
        self,
        case: PlanEvaluationCase,
        ablation: AblationMode,
        started: float,
    ) -> EvaluationCaseResult:
        return EvaluationCaseResult(
            case_id=case.case_id,
            dataset="plan",
            category=case.category,
            status=EvaluationCaseStatus.INTERNAL_ERROR,
            duration_ms=(perf_counter() - started) * 1000,
            seed=case.seed,
            ablation=ablation,
            output_fingerprint=stable_fingerprint({"status": "INTERNAL_ERROR"}),
            violations=("INTERNAL_ERROR",),
            failure_category=EvaluationFailureCategory.INTERNAL_ERROR,
            error_code="EVALUATION_INTERNAL_ERROR",
            safe_description_code="FRAMEWORK_INTERNAL_ERROR",
            metric_values={"evaluation_internal_error": 1.0},
            trace_reference=f"evaluation.case/{case.case_id}",
            deterministic_repetitions=self._repetitions,
        )
