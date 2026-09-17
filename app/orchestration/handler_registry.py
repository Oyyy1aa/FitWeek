"""Explicit handler registration without scanning or decorator side effects."""

from app.domain.orchestration.enums import StepType
from app.orchestration.handler import StepHandler


class HandlerNotRegistered(LookupError):
    code = "HANDLER_NOT_REGISTERED"


class HandlerAlreadyRegistered(ValueError):
    code = "HANDLER_ALREADY_REGISTERED"


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[StepType, StepHandler] = {}

    def register(self, handler: StepHandler) -> None:
        if handler.step_type in self._handlers:
            raise HandlerAlreadyRegistered(
                f"Handler already registered for {handler.step_type.value}."
            )
        if not handler.version.strip():
            raise ValueError("Handler version must not be blank.")
        self._handlers[handler.step_type] = handler

    def get(self, step_type: StepType) -> StepHandler:
        try:
            return self._handlers[step_type]
        except KeyError as exc:
            raise HandlerNotRegistered(
                f"No handler is registered for {step_type.value}."
            ) from exc

    def registered_types(self) -> tuple[StepType, ...]:
        return tuple(sorted(self._handlers, key=lambda item: item.value))
