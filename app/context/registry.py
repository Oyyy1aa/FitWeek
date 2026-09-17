"""Explicit, code-owned Context Contract registry."""

from app.domain.context.contracts import (
    PLAN_GENERATION_CONTEXT,
    PROFILE_AGENT_CONTEXT,
    RECOVERY_AGENT_CONTEXT,
    SCHEDULE_AGENT_CONTEXT,
    SESSION_DESIGNER_CONTEXT,
    ContextContract,
)
from app.domain.context.enums import AgentType
from app.domain.memory.errors import ContextContractVersionNotFoundError


class ContextContractRegistry:
    """Resolve only explicitly registered contracts; never scan or load remotely."""

    def __init__(self) -> None:
        self._contracts: dict[tuple[AgentType, str], ContextContract] = {}
        self._defaults: dict[AgentType, str] = {}

    def register(self, contract: ContextContract, *, default: bool = True) -> None:
        key = (contract.agent_type, contract.version)
        if key in self._contracts:
            raise ValueError("Context Contract type/version is already registered.")
        self._contracts[key] = contract
        if default:
            if contract.agent_type in self._defaults:
                raise ValueError("A default Context Contract is already registered.")
            self._defaults[contract.agent_type] = contract.version

    def get(
        self,
        agent_type: AgentType,
        version: str | None = None,
    ) -> ContextContract:
        selected = version or self._defaults.get(agent_type)
        if selected is None:
            raise ContextContractVersionNotFoundError(
                "No Context Contract version is registered for this Agent."
            )
        try:
            return self._contracts[(agent_type, selected)]
        except KeyError as exc:
            raise ContextContractVersionNotFoundError(
                "The requested Context Contract version is not registered."
            ) from exc


def build_context_contract_registry() -> ContextContractRegistry:
    registry = ContextContractRegistry()
    registry.register(PROFILE_AGENT_CONTEXT)
    registry.register(PLAN_GENERATION_CONTEXT)
    registry.register(SESSION_DESIGNER_CONTEXT)
    registry.register(SCHEDULE_AGENT_CONTEXT)
    registry.register(RECOVERY_AGENT_CONTEXT)
    return registry
