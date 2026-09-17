"""Agent-isolated deterministic Context assembly and audit."""

from __future__ import annotations

from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from app.context.budget import ContextBudget
from app.context.policy import ContextPriorityPolicy
from app.context.registry import ContextContractRegistry
from app.context.serializer import character_count
from app.domain.common import utc_now
from app.domain.context.contracts import ContextContract
from app.domain.context.enums import ContextSectionName
from app.domain.context.models import (
    BuiltContext,
    ContextBuildAudit,
    ContextBuildCommand,
    ContextConflict,
    ContextItem,
    ContextSection,
)
from app.domain.memory.errors import (
    ContextBudgetExceededError,
    ContextContractNotFoundError,
)
from app.domain.memory.models import UserMemory
from app.domain.memory.repositories import MemoryRepository
from app.domain.profiles.models import FitnessProfile, UserConstraint
from app.domain.profiles.repositories import ProfileRepository
from app.memory.metrics import MemoryMetrics
from app.memory.normalization import fingerprint
from app.memory.retrieval import MemoryRetriever


class DeterministicContextBuilder:
    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        memories: MemoryRepository,
        retriever: MemoryRetriever,
        metrics: MemoryMetrics,
        budget: ContextBudget,
        registry: ContextContractRegistry,
        policy: ContextPriorityPolicy | None = None,
    ) -> None:
        self._profiles = profiles
        self._memories = memories
        self._retriever = retriever
        self._metrics = metrics
        self._budget = budget
        self._registry = registry
        self._policy = policy or ContextPriorityPolicy()

    @staticmethod
    def _section(
        name: ContextSectionName,
        source: str,
        version: str,
        items: tuple[ContextItem, ...],
        now: datetime,
    ) -> ContextSection:
        return ContextSection(
            name=name,
            source=source,
            generated_at=now,
            version=version,
            items=items,
        )

    @staticmethod
    def _profile_items(profile: FitnessProfile | None) -> tuple[ContextItem, ...]:
        if profile is None:
            return ()
        return tuple(
            ContextItem(key=key, value=str(value), source="FITNESS_PROFILE")
            for key, value in (
                ("experience_level", profile.experience_level.value),
                ("weekly_frequency", profile.weekly_frequency),
                ("max_session_minutes", profile.max_session_minutes),
                ("primary_goal", profile.primary_goal.value),
                ("scope_confirmed", profile.scope_confirmed),
            )
        )

    @staticmethod
    def _constraint_items(
        constraints: tuple[UserConstraint, ...], now: datetime
    ) -> tuple[ContextItem, ...]:
        active = [
            item
            for item in constraints
            if item.is_hard and (item.valid_until is None or item.valid_until > now)
        ]
        return tuple(
            ContextItem(
                key=item.constraint_type.value,
                value=item.constraint_value,
                source="USER_CONSTRAINT",
                source_reference=str(item.id),
            )
            for item in sorted(
                active,
                key=lambda item: (
                    -item.priority,
                    item.constraint_type.value,
                    item.constraint_value,
                    str(item.id),
                ),
            )
        )

    @staticmethod
    def _memory_item(memory: UserMemory) -> ContextItem:
        return ContextItem(
            key=memory.key,
            value=memory.display_value,
            source="CONFIRMED_MEMORY",
            source_reference=str(memory.id),
        )

    async def build(self, user_id: UUID, command: ContextBuildCommand) -> BuiltContext:
        self._metrics.increment("context_builds")
        now = utc_now()
        try:
            contract = self._registry.get(command.agent_type)
        except ContextContractNotFoundError:
            self._metrics.increment("context_build_failures")
            raise
        profile = await self._profiles.get_by_user_id(user_id)
        constraints = tuple(
            await self._profiles.list_constraints(profile.id) if profile else ()
        )
        retrieval = await self._retriever.retrieve(user_id, now)
        max_characters = min(
            command.max_characters or self._budget.max_characters,
            self._budget.max_characters,
        )
        required = self._required_sections(
            contract=contract,
            command=command,
            profile=profile,
            constraints=constraints,
            now=now,
        )
        required_count = character_count(required)
        if required_count > max_characters:
            self._metrics.increment("context_build_failures")
            self._metrics.increment("context_budget_rejections")
            raise ContextBudgetExceededError(
                "Mandatory Context partitions exceed the character budget."
            )

        exclusion_reasons = dict(retrieval.result.filtered_reasons)
        conflicts: list[ContextConflict] = []
        eligible: list[UserMemory] = []
        profile_values = {item.key: item.value for item in self._profile_items(profile)}
        for memory in retrieval.result.memories:
            if memory.memory_type not in contract.allowed_memory_types:
                exclusion_reasons[memory.id] = "NOT_RELEVANT_TO_AGENT_CONTRACT"
                continue
            conflict = self._policy.conflict(
                memory=memory,
                current_task=command.current_task,
                constraints=constraints,
                profile_values=profile_values,
            )
            if conflict is not None:
                conflicts.append(conflict)
                exclusion_reasons[memory.id] = conflict.resolution
                self._metrics.increment("memory_conflicts_resolved")
                if conflict.higher_priority_source == "CURRENT_TASK":
                    self._metrics.increment("memories_shadowed_by_current_task")
                if conflict.higher_priority_source == "HARD_CONSTRAINTS":
                    self._metrics.increment("memories_shadowed_by_constraints")
                continue
            eligible.append(memory)
        ordered = self._policy.order(eligible, contract)
        all_optional = self._optional_sections(
            contract=contract,
            memories=ordered,
            behavior=command.recent_behavior_summary,
            catalog_reference=command.catalog_reference,
            now=now,
        )
        budget_before = character_count(required + all_optional)
        accepted_behavior: list[str] = []
        if contract.include_behavior_summary and contract.behavior_precedes_memory:
            for item in command.recent_behavior_summary[
                : self._budget.max_behavior_items
            ]:
                candidate = self._behavior_section(
                    (*accepted_behavior, item), contract, now
                )
                if character_count(required + (candidate,)) > max_characters:
                    break
                accepted_behavior.append(item)
        selected: list[UserMemory] = []
        sections = required
        for memory in ordered:
            if len(selected) >= self._budget.max_memories:
                exclusion_reasons[memory.id] = "MEMORY_ITEM_BUDGET"
                continue
            proposal = self._memory_section(tuple((*selected, memory)), contract, now)
            other = self._non_memory_optional_sections(
                contract=contract,
                behavior=tuple(accepted_behavior),
                catalog_reference=None,
                now=now,
            )
            if character_count(required + (proposal,) + other) > max_characters:
                exclusion_reasons[memory.id] = "CONTEXT_CHARACTER_BUDGET"
                continue
            selected.append(memory)
        optional: list[ContextSection] = []
        if accepted_behavior:
            optional.append(
                self._behavior_section(tuple(accepted_behavior), contract, now)
            )
        if selected:
            optional.append(self._memory_section(tuple(selected), contract, now))
        if contract.include_behavior_summary and not contract.behavior_precedes_memory:
            for item in command.recent_behavior_summary[
                : self._budget.max_behavior_items
            ]:
                candidate = self._behavior_section(
                    (*accepted_behavior, item), contract, now
                )
                if (
                    character_count(required + tuple(optional) + (candidate,))
                    > max_characters
                ):
                    break
                accepted_behavior.append(item)
            if accepted_behavior:
                optional.append(
                    self._behavior_section(tuple(accepted_behavior), contract, now)
                )
        if contract.include_catalog_reference and command.catalog_reference:
            tool_section = self._tool_section(command.catalog_reference, contract, now)
            if (
                character_count(required + tuple(optional) + (tool_section,))
                <= max_characters
            ):
                optional.append(tool_section)
        sections = required + tuple(optional)
        included_ids = tuple(item.id for item in selected)
        excluded_ids = tuple(sorted(exclusion_reasons, key=str))
        request_hash = fingerprint(
            {
                "user_id": str(user_id),
                "agent_type": command.agent_type.value,
                "contract_version": contract.version,
                "current_task_fingerprint": fingerprint(dict(command.current_task)),
                "profile_version": profile.version if profile else None,
                "constraint_versions": [item.version for item in constraints],
                "memory_versions": [item.version for item in retrieval.result.memories],
                "budget": max_characters,
            }
        )
        audit_id = uuid5(
            NAMESPACE_URL, f"fitweek:context-audit:{request_hash}:{now.isoformat()}"
        )
        audit = ContextBuildAudit(
            id=audit_id,
            user_id=user_id,
            agent_type=command.agent_type,
            context_contract_version=contract.version,
            request_fingerprint=request_hash,
            included_memory_ids=included_ids,
            excluded_memory_ids=excluded_ids,
            exclusion_reasons=exclusion_reasons,
            conflicts=tuple(conflicts),
            budget_before=budget_before,
            budget_after=character_count(sections),
            degraded_mode=retrieval.degraded_mode,
            created_at=now,
            run_id=command.run_id,
            step_id=command.step_id,
            profile_draft_id=command.profile_draft_id,
            plan_id=command.plan_id,
        )
        await self._memories.save_context_audit(audit)
        return BuiltContext(
            id=uuid5(NAMESPACE_URL, f"fitweek:context:{audit_id}"),
            user_id=user_id,
            agent_type=command.agent_type,
            contract_version=contract.version,
            sections=sections,
            conflicts=tuple(conflicts),
            degraded_mode=retrieval.degraded_mode,
            character_count=audit.budget_after,
            audit_id=audit_id,
            created_at=now,
        )

    def _required_sections(
        self,
        *,
        contract: ContextContract,
        command: ContextBuildCommand,
        profile: FitnessProfile | None,
        constraints: tuple[UserConstraint, ...],
        now: datetime,
    ) -> tuple[ContextSection, ...]:
        task_items = tuple(
            ContextItem(key=key, value=value, source="CURRENT_REQUEST")
            for key, value in sorted(command.current_task.items())
        )
        system = self._section(
            ContextSectionName.SYSTEM_POLICY,
            "INTERNAL_CONTRACT",
            contract.version,
            (
                ContextItem(
                    key="policy", value=contract.system_policy, source="INTERNAL"
                ),
            ),
            now,
        )
        current = self._section(
            ContextSectionName.CURRENT_TASK,
            "CURRENT_REQUEST",
            contract.version,
            task_items,
            now,
        )
        profile_section = self._section(
            ContextSectionName.PROFILE_SNAPSHOT,
            "FITNESS_PROFILE",
            contract.version,
            self._profile_items(profile),
            now,
        )
        hard_constraints = self._section(
            ContextSectionName.HARD_CONSTRAINTS,
            "USER_CONSTRAINTS",
            contract.version,
            self._constraint_items(constraints, now),
            now,
        )
        output = self._section(
            ContextSectionName.OUTPUT_CONTRACT,
            "INTERNAL_CONTRACT",
            contract.version,
            (
                ContextItem(
                    key="output_contract",
                    value=contract.output_contract,
                    source="INTERNAL",
                ),
            ),
            now,
        )
        if contract.behavior_precedes_memory:
            return system, current, hard_constraints, profile_section, output
        return system, current, profile_section, hard_constraints, output

    def _memory_section(
        self,
        memories: tuple[UserMemory, ...],
        contract: ContextContract,
        now: datetime,
    ) -> ContextSection:
        return self._section(
            ContextSectionName.RELEVANT_CONFIRMED_MEMORIES,
            "MEMORY_SERVICE",
            contract.version,
            tuple(self._memory_item(item) for item in memories),
            now,
        )

    def _behavior_section(
        self, behavior: tuple[str, ...], contract: ContextContract, now: datetime
    ) -> ContextSection:
        return self._section(
            ContextSectionName.RECENT_BEHAVIOR_SUMMARY,
            "DETERMINISTIC_PROGRESS_SUMMARY",
            contract.version,
            tuple(
                ContextItem(key=f"item_{index}", value=value, source="BEHAVIOR_SUMMARY")
                for index, value in enumerate(behavior, 1)
            ),
            now,
        )

    def _tool_section(
        self, catalog_reference: str, contract: ContextContract, now: datetime
    ) -> ContextSection:
        return self._section(
            ContextSectionName.TOOL_EVIDENCE,
            "CONTROLLED_CATALOG",
            contract.version,
            (
                ContextItem(
                    key="catalog_reference",
                    value=catalog_reference,
                    source="CONTROLLED_CATALOG",
                ),
            ),
            now,
        )

    def _optional_sections(
        self,
        *,
        contract: ContextContract,
        memories: tuple[UserMemory, ...],
        behavior: tuple[str, ...],
        catalog_reference: str | None,
        now: datetime,
    ) -> tuple[ContextSection, ...]:
        values: list[ContextSection] = []
        if memories:
            values.append(self._memory_section(memories, contract, now))
        values.extend(
            self._non_memory_optional_sections(
                contract=contract,
                behavior=behavior,
                catalog_reference=catalog_reference,
                now=now,
            )
        )
        return tuple(values)

    def _non_memory_optional_sections(
        self,
        *,
        contract: ContextContract,
        behavior: tuple[str, ...],
        catalog_reference: str | None,
        now: datetime,
    ) -> tuple[ContextSection, ...]:
        values: list[ContextSection] = []
        if contract.include_behavior_summary and behavior:
            values.append(self._behavior_section(behavior, contract, now))
        if contract.include_catalog_reference and catalog_reference:
            values.append(self._tool_section(catalog_reference, contract, now))
        return tuple(values)
