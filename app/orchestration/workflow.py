"""Trusted deterministic workflow progression policy."""

from dataclasses import dataclass

from app.domain.orchestration.enums import PlanningRunStatus, StepOutcome, StepType


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowProgression:
    run_status_after: PlanningRunStatus
    next_step_type: StepType | None


_SUCCESS_PROGRESSIONS = {
    StepType.LOAD_PROFILE_CONTEXT: WorkflowProgression(
        run_status_after=PlanningRunStatus.GENERATING_SESSIONS,
        next_step_type=StepType.GENERATE_DETERMINISTIC_PLAN,
    ),
    StepType.GENERATE_DETERMINISTIC_PLAN: WorkflowProgression(
        run_status_after=PlanningRunStatus.ASSEMBLING_PLAN,
        next_step_type=StepType.VERIFY_PLAN_SAFETY,
    ),
    StepType.VERIFY_PLAN_SAFETY: WorkflowProgression(
        run_status_after=PlanningRunStatus.SAFETY_VALIDATING,
        next_step_type=StepType.WAIT_FOR_USER_CONFIRMATION,
    ),
    StepType.FINALIZE_RUN: WorkflowProgression(
        run_status_after=PlanningRunStatus.COMPLETED,
        next_step_type=None,
    ),
    StepType.PARSE_PROFILE_REQUEST: WorkflowProgression(
        run_status_after=PlanningRunStatus.PARSING_PROFILE_REQUEST,
        next_step_type=StepType.WAIT_FOR_PROFILE_DRAFT_REVIEW,
    ),
    StepType.APPLY_PROFILE_DRAFT: WorkflowProgression(
        run_status_after=PlanningRunStatus.FINALIZING_PROFILE_RUN,
        next_step_type=StepType.FINALIZE_PROFILE_RUN,
    ),
    StepType.FINALIZE_PROFILE_RUN: WorkflowProgression(
        run_status_after=PlanningRunStatus.COMPLETED,
        next_step_type=None,
    ),
    StepType.LOAD_SESSION_APPLICATION_CONTEXT: WorkflowProgression(
        run_status_after=PlanningRunStatus.VALIDATING_SESSION_DESIGN_TARGET,
        next_step_type=StepType.VALIDATE_SESSION_DESIGN_TARGET,
    ),
    StepType.VALIDATE_SESSION_DESIGN_TARGET: WorkflowProgression(
        run_status_after=PlanningRunStatus.BUILDING_SESSION_PLAN_REVISION,
        next_step_type=StepType.BUILD_SESSION_PLAN_REVISION,
    ),
    StepType.BUILD_SESSION_PLAN_REVISION: WorkflowProgression(
        run_status_after=PlanningRunStatus.VERIFYING_SESSION_PLAN_SAFETY,
        next_step_type=StepType.VERIFY_SESSION_PLAN_SAFETY,
    ),
    StepType.VERIFY_SESSION_PLAN_SAFETY: WorkflowProgression(
        run_status_after=PlanningRunStatus.VERIFYING_SESSION_PLAN_SAFETY,
        next_step_type=StepType.WAIT_FOR_PLAN_REVISION_CONFIRMATION,
    ),
    StepType.FINALIZE_SESSION_APPLICATION: WorkflowProgression(
        run_status_after=PlanningRunStatus.COMPLETED,
        next_step_type=None,
    ),
    StepType.LOAD_SCHEDULE_APPLICATION_CONTEXT: WorkflowProgression(
        run_status_after=PlanningRunStatus.VALIDATING_SCHEDULE_APPLICATION,
        next_step_type=StepType.VALIDATE_SCHEDULE_APPLICATION,
    ),
    StepType.VALIDATE_SCHEDULE_APPLICATION: WorkflowProgression(
        run_status_after=PlanningRunStatus.REVALIDATING_CALENDAR_BUSY,
        next_step_type=StepType.REVALIDATE_CALENDAR_BUSY,
    ),
    StepType.REVALIDATE_CALENDAR_BUSY: WorkflowProgression(
        run_status_after=PlanningRunStatus.BUILDING_SCHEDULE_PLAN_REVISION,
        next_step_type=StepType.BUILD_SCHEDULE_PLAN_REVISION,
    ),
    StepType.BUILD_SCHEDULE_PLAN_REVISION: WorkflowProgression(
        run_status_after=PlanningRunStatus.VERIFYING_SCHEDULE_PLAN_SAFETY,
        next_step_type=StepType.VERIFY_SCHEDULE_PLAN_SAFETY,
    ),
    StepType.VERIFY_SCHEDULE_PLAN_SAFETY: WorkflowProgression(
        run_status_after=PlanningRunStatus.VERIFYING_SCHEDULE_PLAN_SAFETY,
        next_step_type=StepType.WAIT_FOR_SCHEDULE_REVISION_CONFIRMATION,
    ),
    StepType.FINALIZE_SCHEDULE_APPLICATION: WorkflowProgression(
        run_status_after=PlanningRunStatus.COMPLETED,
        next_step_type=None,
    ),
    StepType.LOAD_CALENDAR_OPERATION_DRAFT: WorkflowProgression(
        run_status_after=PlanningRunStatus.VALIDATING_CALENDAR_OPERATION,
        next_step_type=StepType.VALIDATE_CALENDAR_OPERATION_APPROVAL,
    ),
    StepType.VALIDATE_CALENDAR_OPERATION_APPROVAL: WorkflowProgression(
        run_status_after=PlanningRunStatus.EXECUTING_CALENDAR_OPERATION,
        next_step_type=StepType.EXECUTE_CALENDAR_OPERATION_ITEMS,
    ),
    StepType.EXECUTE_CALENDAR_OPERATION_ITEMS: WorkflowProgression(
        run_status_after=PlanningRunStatus.VERIFYING_CALENDAR_OPERATION,
        next_step_type=StepType.VERIFY_CALENDAR_OPERATION_RESULTS,
    ),
    StepType.VERIFY_CALENDAR_OPERATION_RESULTS: WorkflowProgression(
        run_status_after=PlanningRunStatus.FINALIZING_CALENDAR_OPERATION,
        next_step_type=StepType.FINALIZE_CALENDAR_OPERATION,
    ),
    StepType.FINALIZE_CALENDAR_OPERATION: WorkflowProgression(
        run_status_after=PlanningRunStatus.COMPLETED,
        next_step_type=None,
    ),
    StepType.LOAD_RECOVERY_APPLICATION_CONTEXT: WorkflowProgression(
        run_status_after=PlanningRunStatus.VALIDATING_RECOVERY_DRAFT,
        next_step_type=StepType.VALIDATE_RECOVERY_DRAFT,
    ),
    StepType.VALIDATE_RECOVERY_DRAFT: WorkflowProgression(
        run_status_after=PlanningRunStatus.RESOLVING_RECOVERY_ACTIONS,
        next_step_type=StepType.RESOLVE_RECOVERY_ACTIONS,
    ),
    StepType.RESOLVE_RECOVERY_ACTIONS: WorkflowProgression(
        run_status_after=PlanningRunStatus.CREATING_RECOVERY_SUBDRAFTS,
        next_step_type=StepType.CREATE_RECOVERY_SUBDRAFTS,
    ),
    StepType.CREATE_RECOVERY_SUBDRAFTS: WorkflowProgression(
        run_status_after=PlanningRunStatus.CREATING_RECOVERY_SUBDRAFTS,
        next_step_type=StepType.WAIT_FOR_RECOVERY_SUBDRAFT_REVIEWS,
    ),
    StepType.WAIT_FOR_RECOVERY_SUBDRAFT_REVIEWS: WorkflowProgression(
        run_status_after=PlanningRunStatus.BUILDING_RECOVERY_PLAN_REVISION,
        next_step_type=StepType.BUILD_RECOVERY_PLAN_REVISION,
    ),
    StepType.BUILD_RECOVERY_PLAN_REVISION: WorkflowProgression(
        run_status_after=PlanningRunStatus.VERIFYING_RECOVERY_PLAN_SAFETY,
        next_step_type=StepType.VERIFY_RECOVERY_PLAN_SAFETY,
    ),
    StepType.VERIFY_RECOVERY_PLAN_SAFETY: WorkflowProgression(
        run_status_after=PlanningRunStatus.VERIFYING_RECOVERY_PLAN_SAFETY,
        next_step_type=StepType.WAIT_FOR_RECOVERY_REVISION_CONFIRMATION,
    ),
    StepType.WAIT_FOR_RECOVERY_REVISION_CONFIRMATION: WorkflowProgression(
        run_status_after=PlanningRunStatus.FINALIZING_RECOVERY_APPLICATION,
        next_step_type=StepType.FINALIZE_RECOVERY_APPLICATION,
    ),
    StepType.FINALIZE_RECOVERY_APPLICATION: WorkflowProgression(
        run_status_after=PlanningRunStatus.COMPLETED,
        next_step_type=None,
    ),
}


class DeterministicGenerationWorkflow:
    """Only this policy, not a Handler, selects subsequent states or steps."""

    @staticmethod
    def progression(step_type: StepType, outcome: StepOutcome) -> WorkflowProgression:
        if outcome is StepOutcome.WAITING_USER:
            waiting_status = {
                StepType.WAIT_FOR_USER_CONFIRMATION: (
                    PlanningRunStatus.WAITING_CONFIRMATION
                ),
                StepType.WAIT_FOR_PROFILE_DRAFT_REVIEW: (
                    PlanningRunStatus.WAITING_PROFILE_REVIEW
                ),
                StepType.WAIT_FOR_PLAN_REVISION_CONFIRMATION: (
                    PlanningRunStatus.WAITING_CONFIRMATION
                ),
                StepType.WAIT_FOR_SCHEDULE_REVISION_CONFIRMATION: (
                    PlanningRunStatus.WAITING_CONFIRMATION
                ),
                StepType.WAIT_FOR_RECOVERY_SUBDRAFT_REVIEWS: (
                    PlanningRunStatus.WAITING_SUBDRAFT_REVIEW
                ),
                StepType.WAIT_FOR_RECOVERY_REVISION_CONFIRMATION: (
                    PlanningRunStatus.WAITING_CONFIRMATION
                ),
            }.get(step_type)
            if waiting_status is None:
                raise ValueError("Only an explicit review step may wait for a user.")
            return WorkflowProgression(
                run_status_after=waiting_status,
                next_step_type=None,
            )
        try:
            return _SUCCESS_PROGRESSIONS[step_type]
        except KeyError as exc:
            raise ValueError(
                f"No successful progression exists for {step_type.value}."
            ) from exc
