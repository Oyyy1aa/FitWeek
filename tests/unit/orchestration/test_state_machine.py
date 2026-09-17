"""Explicit Run and Step transitions never accept terminal-state escape."""

from dataclasses import replace

import pytest

from app.domain.orchestration.enums import AgentStepStatus, PlanningRunStatus
from app.domain.orchestration.errors import (
    InvalidRunStateTransition,
    InvalidStepStateTransition,
)
from app.domain.orchestration.state_machine import transition_run, transition_step
from tests.unit.orchestration.factories import make_run, make_step

pytestmark = pytest.mark.phase_2a


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (PlanningRunStatus.CREATED, PlanningRunStatus.QUEUED),
        (PlanningRunStatus.QUEUED, PlanningRunStatus.COLLECTING_PROFILE),
        (
            PlanningRunStatus.COLLECTING_PROFILE,
            PlanningRunStatus.GENERATING_SESSIONS,
        ),
        (
            PlanningRunStatus.GENERATING_SESSIONS,
            PlanningRunStatus.ASSEMBLING_PLAN,
        ),
        (
            PlanningRunStatus.ASSEMBLING_PLAN,
            PlanningRunStatus.SAFETY_VALIDATING,
        ),
        (
            PlanningRunStatus.SAFETY_VALIDATING,
            PlanningRunStatus.WAITING_CONFIRMATION,
        ),
        (
            PlanningRunStatus.WAITING_CONFIRMATION,
            PlanningRunStatus.COMPLETED,
        ),
        (
            PlanningRunStatus.FAILED_RETRYABLE,
            PlanningRunStatus.GENERATING_SESSIONS,
        ),
    ],
)
def test_legal_run_transitions(
    source: PlanningRunStatus,
    target: PlanningRunStatus,
) -> None:
    run = replace(make_run(), status=source)
    assert transition_run(run, target).status is target


@pytest.mark.parametrize(
    "source",
    [
        PlanningRunStatus.CREATED,
        PlanningRunStatus.QUEUED,
        PlanningRunStatus.GENERATING_SESSIONS,
        PlanningRunStatus.WAITING_CONFIRMATION,
    ],
)
def test_nonterminal_run_can_cancel(source: PlanningRunStatus) -> None:
    run = replace(make_run(), status=source)
    assert (
        transition_run(run, PlanningRunStatus.CANCELLED).status
        is PlanningRunStatus.CANCELLED
    )


@pytest.mark.parametrize(
    "terminal",
    [
        PlanningRunStatus.COMPLETED,
        PlanningRunStatus.FAILED_PERMANENT,
        PlanningRunStatus.CANCELLED,
    ],
)
def test_terminal_run_cannot_change(terminal: PlanningRunStatus) -> None:
    with pytest.raises(InvalidRunStateTransition) as caught:
        transition_run(replace(make_run(), status=terminal), PlanningRunStatus.QUEUED)
    assert caught.value.code == "INVALID_RUN_STATE_TRANSITION"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (AgentStepStatus.PENDING, AgentStepStatus.READY),
        (AgentStepStatus.READY, AgentStepStatus.RUNNING),
        (AgentStepStatus.RUNNING, AgentStepStatus.SUCCEEDED),
        (AgentStepStatus.RUNNING, AgentStepStatus.FAILED_RETRYABLE),
        (AgentStepStatus.RUNNING, AgentStepStatus.FAILED_PERMANENT),
        (AgentStepStatus.RUNNING, AgentStepStatus.WAITING_USER),
        (AgentStepStatus.FAILED_RETRYABLE, AgentStepStatus.RETRY_SCHEDULED),
        (AgentStepStatus.RETRY_SCHEDULED, AgentStepStatus.READY),
        (AgentStepStatus.WAITING_USER, AgentStepStatus.SUCCEEDED),
    ],
)
def test_legal_step_transitions(
    source: AgentStepStatus,
    target: AgentStepStatus,
) -> None:
    step = replace(make_step(make_run().id), status=source)
    assert transition_step(step, target).status is target


@pytest.mark.parametrize(
    "terminal",
    [
        AgentStepStatus.SUCCEEDED,
        AgentStepStatus.FAILED_PERMANENT,
        AgentStepStatus.CANCELLED,
    ],
)
def test_terminal_step_cannot_run_again(terminal: AgentStepStatus) -> None:
    step = replace(make_step(make_run().id), status=terminal)
    with pytest.raises(InvalidStepStateTransition) as caught:
        transition_step(step, AgentStepStatus.RUNNING)
    assert caught.value.code == "INVALID_STEP_STATE_TRANSITION"
