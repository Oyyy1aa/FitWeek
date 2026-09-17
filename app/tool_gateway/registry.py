"""Explicit versioned Tool Registry."""

from dataclasses import dataclass

from pydantic import BaseModel

from app.domain.tools.models import ToolDescriptor
from app.domain.tools.protocols import ToolAdapter


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    descriptor: ToolDescriptor
    adapter: ToolAdapter[BaseModel, BaseModel]


class ToolRegistry:
    def __init__(self) -> None:
        self._registrations: dict[tuple[str, str], ToolRegistration] = {}

    def register(
        self, descriptor: ToolDescriptor, adapter: ToolAdapter[BaseModel, BaseModel]
    ) -> None:
        key = (descriptor.tool_id.value, descriptor.version)
        if key in self._registrations:
            raise ValueError("Tool descriptor is already registered.")
        if not issubclass(descriptor.request_model, BaseModel):
            raise TypeError("Tool request model must be a Pydantic model.")
        if not issubclass(descriptor.response_model, BaseModel):
            raise TypeError("Tool response model must be a Pydantic model.")
        self._registrations[key] = ToolRegistration(descriptor, adapter)

    def get(self, tool_id: str, version: str) -> ToolRegistration:
        try:
            return self._registrations[(tool_id, version)]
        except KeyError as exc:
            raise LookupError("TOOL_NOT_REGISTERED") from exc

    def registrations(self) -> tuple[ToolRegistration, ...]:
        return tuple(
            registration
            for _, registration in sorted(
                self._registrations.items(), key=lambda item: item[0]
            )
        )
