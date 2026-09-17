"""Stable orchestration-domain failures."""


class OrchestrationDomainError(RuntimeError):
    code = "ORCHESTRATION_ERROR"


class InvalidRunStateTransition(OrchestrationDomainError):
    code = "INVALID_RUN_STATE_TRANSITION"


class InvalidStepStateTransition(OrchestrationDomainError):
    code = "INVALID_STEP_STATE_TRANSITION"


class RunIdempotencyConflict(OrchestrationDomainError):
    code = "RUN_IDEMPOTENCY_CONFLICT"


class StepNotFound(OrchestrationDomainError):
    code = "STEP_NOT_FOUND"


class StepNotClaimable(OrchestrationDomainError):
    code = "STEP_NOT_CLAIMABLE"


class StepLeaseLost(OrchestrationDomainError):
    code = "STEP_LEASE_LOST"


class CheckpointConflict(OrchestrationDomainError):
    code = "CHECKPOINT_CONFLICT"


class MultipleWaitingSteps(OrchestrationDomainError):
    code = "MULTIPLE_WAITING_STEPS"
