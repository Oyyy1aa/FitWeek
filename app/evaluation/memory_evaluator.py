"""Offline Memory benchmark against isolated in-memory repository adapters."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import TypedDict
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.context.enums import ContextDegradedMode
from app.domain.memory.enums import (
    MemoryCandidateStatus,
    MemoryEvidenceType,
    MemorySource,
    MemoryStatus,
    MemoryType,
)
from app.domain.memory.errors import MemoryCandidateIdempotencyConflictError
from app.domain.memory.models import MemoryCandidate, MemoryEvidence, UserMemory
from app.evaluation.determinism import repetitions_are_identical, stable_fingerprint
from app.evaluation.models import (
    AblationMode,
    EvaluationCaseResult,
    EvaluationCaseStatus,
    EvaluationFailureCategory,
    MemoryEvaluationCase,
)
from app.memory.metrics import MemoryMetrics
from app.memory.retrieval import MemoryRetriever
from app.persistence.memory.memory_repository import InMemoryMemoryRepository
from app.persistence.memory.store import InMemoryStore

REFERENCE_NOW = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)


class _MemoryOutcome(TypedDict):
    business_output: object
    violations: tuple[str, ...]
    failure_category: EvaluationFailureCategory | None
    error_code: str | None
    metrics: dict[str, float]


class MemoryCaseEvaluator:
    def __init__(self, *, repetitions: int = 3, timeout_seconds: float = 10) -> None:
        self._repetitions = repetitions
        self._timeout_seconds = timeout_seconds

    async def evaluate(
        self, case: MemoryEvaluationCase, ablation: AblationMode = AblationMode.FULL
    ) -> EvaluationCaseResult:
        started = perf_counter()
        try:
            repetitions = []
            for _ in range(self._repetitions):
                repetitions.append(
                    await asyncio.wait_for(
                        self._evaluate_once(case, ablation), self._timeout_seconds
                    )
                )
        except TimeoutError:
            return self._error_result(case, ablation, started, timed_out=True)
        except Exception:
            return self._error_result(case, ablation, started, timed_out=False)

        deterministic = repetitions_are_identical(repetitions)
        first = repetitions[0]
        violations = list(first["violations"])
        failure_category = first["failure_category"]
        if not deterministic:
            violations.append("NON_DETERMINISTIC_OUTPUT")
            failure_category = EvaluationFailureCategory.NON_DETERMINISTIC_OUTPUT
        status = (
            EvaluationCaseStatus.PASS if not violations else EvaluationCaseStatus.FAIL
        )
        metrics = dict(first["metrics"])
        metrics["context_deterministic"] = float(deterministic)
        return EvaluationCaseResult(
            case_id=case.case_id,
            dataset="memory",
            category=case.category,
            status=status,
            duration_ms=(perf_counter() - started) * 1000,
            seed=case.seed,
            ablation=ablation,
            output_fingerprint=stable_fingerprint(first["business_output"]),
            violations=tuple(sorted(set(violations))),
            failure_category=failure_category,
            error_code=first["error_code"],
            safe_description_code=(
                None if status is EvaluationCaseStatus.PASS else "MEMORY_CASE_FAILED"
            ),
            metric_values=metrics,
            trace_reference=f"evaluation.case/{case.case_id}",
            deterministic_repetitions=self._repetitions,
        )

    async def _evaluate_once(
        self, case: MemoryEvaluationCase, ablation: AblationMode
    ) -> _MemoryOutcome:
        store = InMemoryStore()
        repository = InMemoryMemoryRepository(store)
        metrics = MemoryMetrics()
        retriever = MemoryRetriever(repository, metrics)
        user_id = uuid5(NAMESPACE_URL, f"fitweek:evaluation:user:{case.case_id}")
        other_user = uuid5(NAMESPACE_URL, f"fitweek:evaluation:other:{case.case_id}")
        scenario = case.input.scenario
        pre_accept_recall = 0
        idempotency_ok = True
        conflict_detected = True
        evidence_ok = True

        if scenario == "ACTIVE_RECALL":
            candidate = self._candidate(case, user_id)
            await repository.create_candidate(
                client_request_id=f"create-{case.case_id}",
                payload_fingerprint=stable_fingerprint({"candidate": case.case_id}),
                candidate=candidate,
            )
            pre_accept_recall = len(
                (
                    await repository.list_active_for_context(user_id, REFERENCE_NOW)
                ).memories
            )
            memory = self._memory(case, user_id, MemoryStatus.ACTIVE)
            evidence = self._evidence(case, memory.id)
            await repository.accept_candidate(
                user_id=user_id,
                candidate_id=candidate.id,
                expected_version=1,
                client_request_id=f"accept-{case.case_id}",
                payload_fingerprint=stable_fingerprint({"accept": case.case_id}),
                confirmed_value=candidate.proposed_value,
                memory=memory,
                evidence=evidence,
                now=REFERENCE_NOW,
            )
        elif scenario == "PENDING_REVIEW":
            await repository.create_candidate(
                client_request_id=f"pending-{case.case_id}",
                payload_fingerprint=stable_fingerprint({"pending": case.case_id}),
                candidate=self._candidate(case, user_id),
            )
        elif scenario == "REJECTED":
            memory = self._memory(case, user_id, MemoryStatus.REJECTED)
            await self._insert(repository, case, memory)
        elif scenario == "DELETED":
            memory = self._memory(case, user_id, MemoryStatus.DELETED)
            await self._insert(repository, case, memory)
        elif scenario == "EXPIRED":
            memory = self._memory(case, user_id, MemoryStatus.EXPIRED)
            await self._insert(repository, case, memory)
        elif scenario == "TTL_BOUNDARY":
            memory = self._memory(
                case, user_id, MemoryStatus.ACTIVE, valid_until=REFERENCE_NOW
            )
            await self._insert(repository, case, memory)
        elif scenario == "DUPLICATE_CANDIDATE":
            candidate = self._candidate(case, user_id)
            fingerprint = stable_fingerprint({"duplicate": case.case_id})
            first = await repository.create_candidate(
                client_request_id=f"duplicate-{case.case_id}",
                payload_fingerprint=fingerprint,
                candidate=candidate,
            )
            second = await repository.create_candidate(
                client_request_id=f"duplicate-{case.case_id}",
                payload_fingerprint=fingerprint,
                candidate=candidate,
            )
            idempotency_ok = (
                first.created
                and not second.created
                and first.candidate.id == second.candidate.id
            )
        elif scenario == "CONFLICT_CANDIDATE":
            candidate = self._candidate(case, user_id)
            request_id = f"conflict-{case.case_id}"
            await repository.create_candidate(
                client_request_id=request_id,
                payload_fingerprint=stable_fingerprint(
                    {"payload": 1, "case": case.case_id}
                ),
                candidate=candidate,
            )
            conflict_detected = False
            try:
                await repository.create_candidate(
                    client_request_id=request_id,
                    payload_fingerprint=stable_fingerprint(
                        {"payload": 2, "case": case.case_id}
                    ),
                    candidate=candidate,
                )
            except MemoryCandidateIdempotencyConflictError:
                conflict_detected = True
        elif scenario == "EVIDENCE":
            memory = self._memory(case, user_id, MemoryStatus.ACTIVE)
            await self._insert(repository, case, memory)
            record = await repository.get_memory(user_id, memory.id, REFERENCE_NOW)
            evidence_ok = record is not None and len(record.evidence) == 1
        elif scenario == "NO_MEMORY":
            repository.set_query_failures(case.input.query_failure_count)
        elif scenario == "CROSS_USER":
            memory = self._memory(case, other_user, MemoryStatus.ACTIVE)
            await self._insert(repository, case, memory)
        elif scenario == "CONTEXT_ORDER":
            older = self._memory(
                case, user_id, MemoryStatus.ACTIVE, index=1, confirmed_offset=-7200
            )
            newer = self._memory(
                case, user_id, MemoryStatus.ACTIVE, index=2, confirmed_offset=-3600
            )
            await self._insert(repository, case, older, suffix="older")
            await self._insert(repository, case, newer, suffix="newer")

        degraded = ContextDegradedMode.NONE
        if scenario == "NO_MEMORY":
            outcome = await retriever.retrieve(user_id, REFERENCE_NOW)
            recalled = outcome.result.memories
            degraded = outcome.degraded_mode
            filtered = outcome.result
        else:
            filtered = await repository.list_active_for_context(user_id, REFERENCE_NOW)
            recalled = filtered.memories

        if ablation is AblationMode.NO_MEMORY_CONTEXT:
            recalled = ()

        expected = case.input.expected_recall_count
        violations: list[str] = []
        failure_category: EvaluationFailureCategory | None = None
        if len(recalled) != expected:
            failure_category = self._leak_category(scenario)
            violations.append(failure_category.value)
        if scenario == "ACTIVE_RECALL" and pre_accept_recall != 0:
            failure_category = EvaluationFailureCategory.PENDING_MEMORY_LEAK
            violations.append("PENDING_MEMORY_LEAK")
        if not idempotency_ok:
            failure_category = EvaluationFailureCategory.IDEMPOTENCY_FAILURE
            violations.append("IDEMPOTENCY_FAILURE")
        if not conflict_detected:
            failure_category = EvaluationFailureCategory.CONFLICT_NOT_DETECTED
            violations.append("CONFLICT_NOT_DETECTED")
        if not evidence_ok:
            failure_category = EvaluationFailureCategory.SETUP_FAILED
            violations.append("EVIDENCE_VALIDATION_FAILED")
        if scenario == "NO_MEMORY" and degraded is not ContextDegradedMode.NO_MEMORY:
            failure_category = EvaluationFailureCategory.NO_MEMORY_DEGRADATION_FAILED
            violations.append("NO_MEMORY_DEGRADATION_FAILED")

        recalled_ids = [str(item.id) for item in recalled[: case.input.context_limit]]
        if scenario == "CONTEXT_ORDER" and len(recalled) == 2:
            if (
                recalled[0].confirmed_at is None
                or recalled[1].confirmed_at is None
                or recalled[0].confirmed_at < recalled[1].confirmed_at
            ):
                failure_category = EvaluationFailureCategory.CONTEXT_ORDER_MISMATCH
                violations.append("CONTEXT_ORDER_MISMATCH")

        metrics_map = {
            "active_memory_recall_accurate": float(len(recalled) == expected),
            "pending_memory_leak": float(
                scenario == "PENDING_REVIEW" and bool(recalled)
            ),
            "expired_memory_recall": float(
                scenario in {"EXPIRED", "TTL_BOUNDARY"} and bool(recalled)
            ),
            "deleted_memory_recall": float(scenario == "DELETED" and bool(recalled)),
            "cross_user_memory_leak": float(
                scenario == "CROSS_USER" and bool(recalled)
            ),
            "memory_candidate_idempotent": float(idempotency_ok),
            "memory_conflict_detected": float(conflict_detected),
            "no_memory_degradation_succeeded": float(
                scenario != "NO_MEMORY" or degraded is ContextDegradedMode.NO_MEMORY
            ),
            "unexpected_side_effect": 0.0,
        }
        business_output = {
            "scenario": scenario,
            "recalled_ids": recalled_ids,
            "expired_filtered": filtered.expired_filtered,
            "deleted_filtered": filtered.deleted_filtered,
            "pending_filtered": filtered.pending_filtered,
            "degraded_mode": degraded.value,
            "candidate_idempotent": idempotency_ok,
            "conflict_detected": conflict_detected,
            "evidence_valid": evidence_ok,
        }
        return {
            "business_output": business_output,
            "violations": tuple(violations),
            "failure_category": failure_category,
            "error_code": None if not violations else violations[0],
            "metrics": metrics_map,
        }

    @staticmethod
    def _memory(
        case: MemoryEvaluationCase,
        user_id: UUID,
        status: MemoryStatus,
        *,
        index: int = 1,
        valid_until: datetime | None = None,
        confirmed_offset: int = -3600,
    ) -> UserMemory:
        memory_id = uuid5(
            NAMESPACE_URL, f"fitweek:evaluation:memory:{case.case_id}:{index}"
        )
        valid_from = REFERENCE_NOW - timedelta(days=2)
        if valid_until is None:
            if status is MemoryStatus.EXPIRED:
                valid_until = REFERENCE_NOW - timedelta(days=1)
            else:
                offset = case.input.valid_until_offset_seconds
                valid_until = (
                    None
                    if offset is None
                    else REFERENCE_NOW + timedelta(seconds=offset)
                )
        confirmed = (
            REFERENCE_NOW + timedelta(seconds=confirmed_offset)
            if status
            in {MemoryStatus.ACTIVE, MemoryStatus.EXPIRED, MemoryStatus.DELETED}
            else None
        )
        return UserMemory(
            id=memory_id,
            user_id=user_id,
            memory_type=(
                MemoryType.PREFERRED_LOCATION
                if index == 1
                else MemoryType.PREFERRED_TIME_OF_DAY
            ),
            key="preferred_location" if index == 1 else "preferred_time_of_day",
            normalized_value="home" if index == 1 else "morning",
            display_value="HOME" if index == 1 else "MORNING",
            status=status,
            source=MemorySource.PROFILE_AGENT_CANDIDATE,
            confidence=Decimal("0.9"),
            valid_from=valid_from,
            valid_until=valid_until,
            confirmed_at=confirmed,
            created_at=valid_from,
            updated_at=REFERENCE_NOW - timedelta(hours=1),
            deleted_at=(
                REFERENCE_NOW - timedelta(minutes=30)
                if status is MemoryStatus.DELETED
                else None
            ),
            version=1,
        )

    @staticmethod
    def _evidence(case: MemoryEvaluationCase, memory_id: UUID) -> MemoryEvidence:
        return MemoryEvidence(
            id=uuid5(
                NAMESPACE_URL, f"fitweek:evaluation:evidence:{case.case_id}:{memory_id}"
            ),
            memory_id=memory_id,
            evidence_type=MemoryEvidenceType.PROFILE_DRAFT,
            source_reference=f"evaluation:{case.description_code}",
            evidence_summary="Synthetic structured preference confirmation.",
            source_occurred_at=REFERENCE_NOW - timedelta(hours=1),
            created_at=REFERENCE_NOW - timedelta(hours=1),
            content_fingerprint=stable_fingerprint(
                {"evidence": case.case_id, "memory": str(memory_id)}
            ),
        )

    @staticmethod
    def _candidate(case: MemoryEvaluationCase, user_id: UUID) -> MemoryCandidate:
        return MemoryCandidate(
            id=uuid5(NAMESPACE_URL, f"fitweek:evaluation:candidate:{case.case_id}"),
            user_id=user_id,
            memory_type=MemoryType.PREFERRED_LOCATION,
            proposed_key="preferred_location",
            proposed_value="home",
            source=MemorySource.PROFILE_AGENT_CANDIDATE,
            source_reference=f"evaluation:{case.description_code}",
            evidence_summary="Synthetic structured proposal.",
            confidence=Decimal("0.8"),
            status=MemoryCandidateStatus.PENDING_REVIEW,
            created_at=REFERENCE_NOW - timedelta(hours=1),
            expires_at=REFERENCE_NOW + timedelta(days=1),
            reviewed_at=None,
            version=1,
        )

    async def _insert(
        self,
        repository: InMemoryMemoryRepository,
        case: MemoryEvaluationCase,
        memory: UserMemory,
        *,
        suffix: str = "record",
    ) -> None:
        await repository.create_memory(
            client_request_id=f"{suffix}-{case.case_id}",
            payload_fingerprint=stable_fingerprint({suffix: case.case_id}),
            memory=memory,
            evidence=self._evidence(case, memory.id),
        )

    @staticmethod
    def _leak_category(scenario: str) -> EvaluationFailureCategory:
        if scenario == "PENDING_REVIEW":
            return EvaluationFailureCategory.PENDING_MEMORY_LEAK
        if scenario in {"EXPIRED", "TTL_BOUNDARY"}:
            return EvaluationFailureCategory.EXPIRED_MEMORY_LEAK
        if scenario == "DELETED":
            return EvaluationFailureCategory.DELETED_MEMORY_LEAK
        if scenario == "CROSS_USER":
            return EvaluationFailureCategory.CROSS_USER_LEAK
        return EvaluationFailureCategory.EXPECTED_REJECTION_MISMATCH

    def _error_result(
        self,
        case: MemoryEvaluationCase,
        ablation: AblationMode,
        started: float,
        *,
        timed_out: bool,
    ) -> EvaluationCaseResult:
        status = (
            EvaluationCaseStatus.TIMED_OUT
            if timed_out
            else EvaluationCaseStatus.INTERNAL_ERROR
        )
        category = (
            EvaluationFailureCategory.TIMEOUT
            if timed_out
            else EvaluationFailureCategory.INTERNAL_ERROR
        )
        return EvaluationCaseResult(
            case_id=case.case_id,
            dataset="memory",
            category=case.category,
            status=status,
            duration_ms=(perf_counter() - started) * 1000,
            seed=case.seed,
            ablation=ablation,
            output_fingerprint=stable_fingerprint({"status": status.value}),
            violations=(category.value,),
            failure_category=category,
            error_code="EVALUATION_CASE_TIMEOUT"
            if timed_out
            else "EVALUATION_INTERNAL_ERROR",
            safe_description_code="CASE_TIMEOUT"
            if timed_out
            else "FRAMEWORK_INTERNAL_ERROR",
            metric_values={
                "case_timeout" if timed_out else "evaluation_internal_error": 1.0
            },
            trace_reference=f"evaluation.case/{case.case_id}",
            deterministic_repetitions=self._repetitions,
        )
