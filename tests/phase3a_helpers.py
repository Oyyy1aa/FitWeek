"""Reusable Phase 3A contract fixtures without external services."""

import json
from collections.abc import Iterable
from datetime import date
from uuid import UUID

from app.agents.profile_agent import ProfileAgent
from app.domain.common import LocationType
from app.domain.model_gateway.errors import ModelGatewayError
from app.domain.model_gateway.models import ModelProviderResponse
from app.domain.profile_agent.models import ProfileAgentInput
from app.domain.profiles.models import ConstraintType, FitnessGoal
from app.model_gateway.fake_provider import FakeAction, ScriptedFakeProvider
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.limiter import ProcessLocalModelLimiter
from app.model_gateway.retry_policy import ModelRetryPolicy
from app.prompts.profile_agent import PROFILE_AGENT_PROMPT_V1
from app.prompts.registry import PromptRegistry

TEST_USER_ID = UUID("00000000-0000-4000-8000-000000000001")


class NoSleep:
    async def sleep(self, seconds: float) -> None:
        del seconds


def output_document(
    *,
    scope_status: str = "SUPPORTED",
    category: str = "complete",
) -> dict[str, object]:
    document: dict[str, object] = {
        "weekly_frequency": 3,
        "max_session_minutes": 30,
        "goals": ["GENERAL_FITNESS"],
        "hard_constraints": [],
        "soft_preferences": [],
        "temporary_constraints": [],
        "equipment": ["resistance_band"],
        "locations": ["HOME"],
        "scope_status": scope_status,
        "missing_fields": [],
        "memory_candidates": [],
        "explanation_summary": "Structured suggestions require user review.",
    }
    if category == "missing":
        document.update(
            {
                "weekly_frequency": None,
                "max_session_minutes": None,
                "goals": [],
                "equipment": [],
                "locations": [],
                "scope_status": "NEEDS_REVIEW",
                "missing_fields": ["weekly_frequency", "primary_goal"],
            }
        )
    elif category == "temporary":
        document["temporary_constraints"] = [
            {
                "constraint_type": "AVAILABLE_EQUIPMENT",
                "value": "none",
                "is_hard": True,
                "priority": 80,
                "valid_until": None,
            }
        ]
    elif category == "long_term":
        document["memory_candidates"] = [
            {
                "category": "PREFERENCE",
                "value": "prefers morning training",
                "rationale": "The user explicitly described a long-term preference.",
            }
        ]
    elif category == "hard":
        document["hard_constraints"] = [
            {
                "constraint_type": "EXCLUDED_FEATURE",
                "value": "jumping",
                "is_hard": True,
                "priority": 100,
                "valid_until": None,
            }
        ]
    return document


def output_json(**values: str) -> str:
    return json.dumps(output_document(**values), ensure_ascii=False)


def build_gateway(
    *,
    primary_script: Iterable[FakeAction] = (),
    backup_script: Iterable[FakeAction] | None = None,
    max_attempts: int = 3,
) -> tuple[ModelGateway, ScriptedFakeProvider, ScriptedFakeProvider | None]:
    primary = ScriptedFakeProvider(
        provider_name="primary-fake",
        script=primary_script,
        default_raw_text=output_json(),
    )
    backup = (
        ScriptedFakeProvider(
            provider_name="backup-fake",
            script=backup_script,
            default_raw_text=output_json(),
        )
        if backup_script is not None
        else None
    )
    gateway = ModelGateway(
        primary=primary,
        primary_model="primary-model",
        backup=backup,
        backup_model="backup-model",
        retry_policy=ModelRetryPolicy(max_attempts=max_attempts, retry_delay_seconds=0),
        limiter=ProcessLocalModelLimiter(
            max_concurrency=4,
            rate_per_minute=1000,
            acquire_timeout_seconds=1,
        ),
        sleeper=NoSleep(),
    )
    return gateway, primary, backup


def build_agent(gateway: ModelGateway) -> ProfileAgent:
    registry = PromptRegistry()
    registry.register(PROFILE_AGENT_PROMPT_V1)
    return ProfileAgent(gateway=gateway, prompts=registry)


def agent_input(message: str = "每周三次，每次30分钟，居家训练") -> ProfileAgentInput:
    return ProfileAgentInput(
        user_message=message,
        current_week=date(2026, 7, 13),
        existing_profile=None,
        supported_goals=tuple(item.value for item in FitnessGoal),
        supported_constraint_types=tuple(item.value for item in ConstraintType),
        supported_equipment=("chair", "dumbbell", "resistance_band", "yoga_mat"),
        supported_locations=tuple(item.value for item in LocationType),
    )


__all__ = [
    "FakeAction",
    "ModelGatewayError",
    "ModelProviderResponse",
    "TEST_USER_ID",
    "agent_input",
    "build_agent",
    "build_gateway",
    "output_document",
    "output_json",
]
