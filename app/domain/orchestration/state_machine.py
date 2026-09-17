"""Explicit, side-effect-free Run and Step state machines."""

from dataclasses import replace

from app.domain.orchestration.enums import AgentStepStatus, PlanningRunStatus
from app.domain.orchestration.errors import (
    InvalidRunStateTransition,
    InvalidStepStateTransition,
)
from app.domain.orchestration.models import AgentStep, PlanningRun

RUN_TERMINAL_STATUSES = frozenset(
    {
        PlanningRunStatus.COMPLETED,
        PlanningRunStatus.FAILED_PERMANENT,
        PlanningRunStatus.CANCELLED,
    }
)
STEP_TERMINAL_STATUSES = frozenset(
    {
        AgentStepStatus.SUCCEEDED,
        AgentStepStatus.FAILED_PERMANENT,
        AgentStepStatus.SKIPPED,
        AgentStepStatus.CANCELLED,
    }
)

RUN_TRANSITIONS: dict[PlanningRunStatus, frozenset[PlanningRunStatus]] = {
    PlanningRunStatus.CREATED: frozenset({PlanningRunStatus.QUEUED}),
    PlanningRunStatus.QUEUED: frozenset(
        {
            PlanningRunStatus.COLLECTING_PROFILE,
            PlanningRunStatus.PARSING_PROFILE_REQUEST,
            PlanningRunStatus.LOADING_SESSION_APPLICATION_CONTEXT,
            PlanningRunStatus.LOADING_SCHEDULE_APPLICATION_CONTEXT,
            PlanningRunStatus.LOADING_CALENDAR_OPERATION,
            PlanningRunStatus.LOADING_RECOVERY_APPLICATION_CONTEXT,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.COLLECTING_PROFILE: frozenset(
        {
            PlanningRunStatus.GENERATING_SESSIONS,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.GENERATING_SESSIONS: frozenset(
        {
            PlanningRunStatus.ASSEMBLING_PLAN,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.ASSEMBLING_PLAN: frozenset(
        {
            PlanningRunStatus.SAFETY_VALIDATING,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.SAFETY_VALIDATING: frozenset(
        {
            PlanningRunStatus.WAITING_CONFIRMATION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.WAITING_CONFIRMATION: frozenset(
        {
            PlanningRunStatus.COMPLETED,
            PlanningRunStatus.FINALIZING_SESSION_APPLICATION,
            PlanningRunStatus.FINALIZING_SCHEDULE_APPLICATION,
            PlanningRunStatus.FINALIZING_RECOVERY_APPLICATION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.PARSING_PROFILE_REQUEST: frozenset(
        {
            PlanningRunStatus.WAITING_PROFILE_REVIEW,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.WAITING_PROFILE_REVIEW: frozenset(
        {
            PlanningRunStatus.APPLYING_PROFILE_DRAFT,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.APPLYING_PROFILE_DRAFT: frozenset(
        {
            PlanningRunStatus.FINALIZING_PROFILE_RUN,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.FINALIZING_PROFILE_RUN: frozenset(
        {
            PlanningRunStatus.COMPLETED,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.LOADING_SESSION_APPLICATION_CONTEXT: frozenset(
        {
            PlanningRunStatus.VALIDATING_SESSION_DESIGN_TARGET,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.VALIDATING_SESSION_DESIGN_TARGET: frozenset(
        {
            PlanningRunStatus.BUILDING_SESSION_PLAN_REVISION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.BUILDING_SESSION_PLAN_REVISION: frozenset(
        {
            PlanningRunStatus.VERIFYING_SESSION_PLAN_SAFETY,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.VERIFYING_SESSION_PLAN_SAFETY: frozenset(
        {
            PlanningRunStatus.WAITING_CONFIRMATION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.FINALIZING_SESSION_APPLICATION: frozenset(
        {
            PlanningRunStatus.COMPLETED,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.LOADING_SCHEDULE_APPLICATION_CONTEXT: frozenset(
        {
            PlanningRunStatus.VALIDATING_SCHEDULE_APPLICATION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.VALIDATING_SCHEDULE_APPLICATION: frozenset(
        {
            PlanningRunStatus.REVALIDATING_CALENDAR_BUSY,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.REVALIDATING_CALENDAR_BUSY: frozenset(
        {
            PlanningRunStatus.BUILDING_SCHEDULE_PLAN_REVISION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.BUILDING_SCHEDULE_PLAN_REVISION: frozenset(
        {
            PlanningRunStatus.VERIFYING_SCHEDULE_PLAN_SAFETY,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.VERIFYING_SCHEDULE_PLAN_SAFETY: frozenset(
        {
            PlanningRunStatus.WAITING_CONFIRMATION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.FINALIZING_SCHEDULE_APPLICATION: frozenset(
        {
            PlanningRunStatus.COMPLETED,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.LOADING_CALENDAR_OPERATION: frozenset(
        {
            PlanningRunStatus.VALIDATING_CALENDAR_OPERATION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.VALIDATING_CALENDAR_OPERATION: frozenset(
        {
            PlanningRunStatus.EXECUTING_CALENDAR_OPERATION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.EXECUTING_CALENDAR_OPERATION: frozenset(
        {
            PlanningRunStatus.VERIFYING_CALENDAR_OPERATION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.VERIFYING_CALENDAR_OPERATION: frozenset(
        {
            PlanningRunStatus.FINALIZING_CALENDAR_OPERATION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.FINALIZING_CALENDAR_OPERATION: frozenset(
        {
            PlanningRunStatus.COMPLETED,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.LOADING_RECOVERY_APPLICATION_CONTEXT: frozenset(
        {
            PlanningRunStatus.VALIDATING_RECOVERY_DRAFT,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.VALIDATING_RECOVERY_DRAFT: frozenset(
        {
            PlanningRunStatus.RESOLVING_RECOVERY_ACTIONS,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.RESOLVING_RECOVERY_ACTIONS: frozenset(
        {
            PlanningRunStatus.CREATING_RECOVERY_SUBDRAFTS,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.CREATING_RECOVERY_SUBDRAFTS: frozenset(
        {
            PlanningRunStatus.WAITING_SUBDRAFT_REVIEW,
            PlanningRunStatus.BUILDING_RECOVERY_PLAN_REVISION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.WAITING_SUBDRAFT_REVIEW: frozenset(
        {
            PlanningRunStatus.BUILDING_RECOVERY_PLAN_REVISION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.BUILDING_RECOVERY_PLAN_REVISION: frozenset(
        {
            PlanningRunStatus.VERIFYING_RECOVERY_PLAN_SAFETY,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.VERIFYING_RECOVERY_PLAN_SAFETY: frozenset(
        {
            PlanningRunStatus.WAITING_CONFIRMATION,
            PlanningRunStatus.FINALIZING_RECOVERY_APPLICATION,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.FINALIZING_RECOVERY_APPLICATION: frozenset(
        {
            PlanningRunStatus.COMPLETED,
            PlanningRunStatus.FAILED_RETRYABLE,
        }
    ),
    PlanningRunStatus.FAILED_RETRYABLE: frozenset(
        {
            PlanningRunStatus.COLLECTING_PROFILE,
            PlanningRunStatus.GENERATING_SESSIONS,
            PlanningRunStatus.ASSEMBLING_PLAN,
            PlanningRunStatus.SAFETY_VALIDATING,
            PlanningRunStatus.WAITING_CONFIRMATION,
            PlanningRunStatus.PARSING_PROFILE_REQUEST,
            PlanningRunStatus.WAITING_PROFILE_REVIEW,
            PlanningRunStatus.APPLYING_PROFILE_DRAFT,
            PlanningRunStatus.FINALIZING_PROFILE_RUN,
            PlanningRunStatus.LOADING_SESSION_APPLICATION_CONTEXT,
            PlanningRunStatus.VALIDATING_SESSION_DESIGN_TARGET,
            PlanningRunStatus.BUILDING_SESSION_PLAN_REVISION,
            PlanningRunStatus.VERIFYING_SESSION_PLAN_SAFETY,
            PlanningRunStatus.FINALIZING_SESSION_APPLICATION,
            PlanningRunStatus.LOADING_SCHEDULE_APPLICATION_CONTEXT,
            PlanningRunStatus.VALIDATING_SCHEDULE_APPLICATION,
            PlanningRunStatus.REVALIDATING_CALENDAR_BUSY,
            PlanningRunStatus.BUILDING_SCHEDULE_PLAN_REVISION,
            PlanningRunStatus.VERIFYING_SCHEDULE_PLAN_SAFETY,
            PlanningRunStatus.FINALIZING_SCHEDULE_APPLICATION,
            PlanningRunStatus.LOADING_CALENDAR_OPERATION,
            PlanningRunStatus.VALIDATING_CALENDAR_OPERATION,
            PlanningRunStatus.EXECUTING_CALENDAR_OPERATION,
            PlanningRunStatus.VERIFYING_CALENDAR_OPERATION,
            PlanningRunStatus.FINALIZING_CALENDAR_OPERATION,
            PlanningRunStatus.LOADING_RECOVERY_APPLICATION_CONTEXT,
            PlanningRunStatus.VALIDATING_RECOVERY_DRAFT,
            PlanningRunStatus.RESOLVING_RECOVERY_ACTIONS,
            PlanningRunStatus.CREATING_RECOVERY_SUBDRAFTS,
            PlanningRunStatus.WAITING_SUBDRAFT_REVIEW,
            PlanningRunStatus.BUILDING_RECOVERY_PLAN_REVISION,
            PlanningRunStatus.VERIFYING_RECOVERY_PLAN_SAFETY,
            PlanningRunStatus.FINALIZING_RECOVERY_APPLICATION,
            PlanningRunStatus.FAILED_PERMANENT,
        }
    ),
    PlanningRunStatus.COMPLETED: frozenset(),
    PlanningRunStatus.FAILED_PERMANENT: frozenset(),
    PlanningRunStatus.CANCELLED: frozenset(),
}

STEP_TRANSITIONS: dict[AgentStepStatus, frozenset[AgentStepStatus]] = {
    AgentStepStatus.PENDING: frozenset(
        {AgentStepStatus.READY, AgentStepStatus.CANCELLED}
    ),
    AgentStepStatus.READY: frozenset(
        {AgentStepStatus.RUNNING, AgentStepStatus.CANCELLED}
    ),
    AgentStepStatus.RUNNING: frozenset(
        {
            AgentStepStatus.SUCCEEDED,
            AgentStepStatus.FAILED_RETRYABLE,
            AgentStepStatus.FAILED_PERMANENT,
            AgentStepStatus.WAITING_USER,
            AgentStepStatus.CANCELLED,
        }
    ),
    AgentStepStatus.FAILED_RETRYABLE: frozenset(
        {AgentStepStatus.RETRY_SCHEDULED, AgentStepStatus.FAILED_PERMANENT}
    ),
    AgentStepStatus.RETRY_SCHEDULED: frozenset(
        {AgentStepStatus.READY, AgentStepStatus.CANCELLED}
    ),
    AgentStepStatus.WAITING_USER: frozenset(
        {AgentStepStatus.SUCCEEDED, AgentStepStatus.CANCELLED}
    ),
    AgentStepStatus.SUCCEEDED: frozenset(),
    AgentStepStatus.FAILED_PERMANENT: frozenset(),
    AgentStepStatus.SKIPPED: frozenset(),
    AgentStepStatus.CANCELLED: frozenset(),
}


def transition_run(run: PlanningRun, target: PlanningRunStatus) -> PlanningRun:
    if (
        target is PlanningRunStatus.CANCELLED
        and run.status not in RUN_TERMINAL_STATUSES
    ):
        return replace(run, status=target)
    if (
        target is PlanningRunStatus.FAILED_PERMANENT
        and run.status not in RUN_TERMINAL_STATUSES
    ):
        return replace(run, status=target)
    if target not in RUN_TRANSITIONS[run.status]:
        raise InvalidRunStateTransition(
            f"Run cannot transition from {run.status.value} to {target.value}."
        )
    return replace(run, status=target)


def transition_step(step: AgentStep, target: AgentStepStatus) -> AgentStep:
    if target not in STEP_TRANSITIONS[step.status]:
        raise InvalidStepStateTransition(
            f"Step cannot transition from {step.status.value} to {target.value}."
        )
    return replace(step, status=target)
