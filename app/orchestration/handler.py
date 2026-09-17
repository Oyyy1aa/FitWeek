"""Handler contract: bounded deterministic work outside repository locks."""

from dataclasses import dataclass
from typing import Protocol

from app.domain.orchestration.enums import StepOutcome, StepType
from app.domain.orchestration.models import ClaimedStep, JsonObject, safe_json_object


class StepExecutionError(RuntimeError):
    code = "STEP_EXECUTION_ERROR"


class RetryableStepError(StepExecutionError):
    code = "RETRYABLE_STEP_ERROR"


class PermanentStepError(StepExecutionError):
    code = "PERMANENT_STEP_ERROR"


class StepTimeoutError(RetryableStepError):
    code = "STEP_HANDLER_TIMEOUT"


class HandlerContractError(PermanentStepError):
    code = "HANDLER_CONTRACT_VIOLATION"


@dataclass(frozen=True, slots=True, kw_only=True)
class StepExecutionContext:
    claim: ClaimedStep


@dataclass(frozen=True, slots=True, kw_only=True)
class StepExecutionResult:
    outcome: StepOutcome
    output_payload: JsonObject
    result_reference: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, StepOutcome):
            raise HandlerContractError("Handler outcome is invalid.")
        object.__setattr__(
            self,
            "output_payload",
            safe_json_object(self.output_payload),
        )


class StepHandler(Protocol):
    step_type: StepType
    version: str

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult: ...
