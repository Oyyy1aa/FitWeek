"""Versioned prompt for candidate-ID-only Recovery selection."""

from app.prompts.registry import PromptDefinition

SYSTEM = """You are the FitWeek controlled Recovery Agent.
Select only IDs from allowed_action_candidates. Never invent an action, time,
exercise, plan patch, calendar operation, memory, medical interpretation, or tool call.
Current request outranks behavior patterns; hard constraints outrank all preferences.
Return exactly one JSON object matching RecoveryAgentOutput.v1 with no extra fields."""

USER = """Frozen recovery input:
{context_json}
Current request marker: {user_message_json}
Return selected_action_candidate_ids, unresolved_session_ids, and a short
non-medical explanation_summary."""

RECOVERY_AGENT_PROMPT_V1 = PromptDefinition.create(
    name="recovery-agent",
    version="recovery-agent-v1",
    system_template=SYSTEM,
    user_template=USER,
    response_schema_name="RecoveryAgentOutput.v1",
)
