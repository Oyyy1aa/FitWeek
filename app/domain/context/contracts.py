"""Agent-specific Context allowlists and internal-only policy text."""

from dataclasses import dataclass

from app.domain.context.enums import AgentType
from app.domain.memory.enums import MemoryType


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextContract:
    agent_type: AgentType
    version: str
    policy_version: str
    system_policy: str
    output_contract: str
    allowed_memory_types: tuple[MemoryType, ...]
    include_behavior_summary: bool
    include_catalog_reference: bool
    behavior_precedes_memory: bool = False


PROFILE_AGENT_CONTEXT = ContextContract(
    agent_type=AgentType.PROFILE_AGENT,
    version="profile-agent-context-v1",
    policy_version="context-policy-v1",
    system_policy=(
        "Parse only supported fitness-profile facts; do not diagnose or write "
        "business state."
    ),
    output_contract=(
        "Return only the registered Profile Agent structured Draft contract."
    ),
    allowed_memory_types=(
        MemoryType.PREFERRED_LOCATION,
        MemoryType.PREFERRED_TIME_OF_DAY,
        MemoryType.DISLIKED_ACTIVITY,
        MemoryType.PREFERRED_EQUIPMENT,
    ),
    include_behavior_summary=False,
    include_catalog_reference=False,
)

PLAN_GENERATION_CONTEXT = ContextContract(
    agent_type=AgentType.PLAN_GENERATION,
    version="plan-generation-context-v1",
    policy_version="context-policy-v1",
    system_policy="Use controlled exercises and obey all current hard constraints.",
    output_contract=(
        "Return a structured plan proposal using controlled catalog identifiers."
    ),
    allowed_memory_types=tuple(MemoryType),
    include_behavior_summary=True,
    include_catalog_reference=True,
)

SESSION_DESIGNER_CONTEXT = ContextContract(
    agent_type=AgentType.SESSION_DESIGNER,
    version="session-designer-context-v1",
    policy_version="context-policy-v1",
    system_policy=(
        "Select only exercises in the frozen Candidate Set and obey current "
        "hard constraints."
    ),
    output_contract="Return only the registered Session Designer structured contract.",
    allowed_memory_types=(
        MemoryType.PREFERRED_LOCATION,
        MemoryType.DISLIKED_ACTIVITY,
        MemoryType.PREFERRED_EQUIPMENT,
    ),
    include_behavior_summary=False,
    include_catalog_reference=True,
)

SCHEDULE_AGENT_CONTEXT = ContextContract(
    agent_type=AgentType.SCHEDULE_AGENT,
    version="schedule-agent-context-v1",
    policy_version="context-policy-v1",
    system_policy=(
        "Select only frozen time-slot IDs. Hard constraints and Session invariants "
        "override confirmed time or location preferences."
    ),
    output_contract="Return only the registered Schedule Agent structured contract.",
    allowed_memory_types=(
        MemoryType.PREFERRED_TIME_OF_DAY,
        MemoryType.PREFERRED_LOCATION,
    ),
    include_behavior_summary=False,
    include_catalog_reference=False,
)

RECOVERY_AGENT_CONTEXT = ContextContract(
    agent_type=AgentType.RECOVERY_AGENT,
    version="recovery-agent-context-v1",
    policy_version="context-policy-v1",
    system_policy=(
        "Select only frozen Recovery Candidate IDs. Current requests and hard "
        "constraints override behavior patterns and confirmed preferences."
    ),
    output_contract="Return only the registered Recovery Agent structured contract.",
    allowed_memory_types=(
        MemoryType.PREFERRED_TIME_OF_DAY,
        MemoryType.PREFERRED_LOCATION,
        MemoryType.PREFERRED_EQUIPMENT,
    ),
    include_behavior_summary=True,
    include_catalog_reference=False,
    behavior_precedes_memory=True,
)

CONTEXT_CONTRACTS = {
    (item.agent_type, item.version): item
    for item in (
        PROFILE_AGENT_CONTEXT,
        PLAN_GENERATION_CONTEXT,
        SESSION_DESIGNER_CONTEXT,
        SCHEDULE_AGENT_CONTEXT,
        RECOVERY_AGENT_CONTEXT,
    )
}
