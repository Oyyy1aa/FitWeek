"""Typed, privacy-minimal evaluation contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvaluationCaseStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    TIMED_OUT = "TIMED_OUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class EvaluationFailureCategory(StrEnum):
    DATASET_INVALID = "DATASET_INVALID"
    SETUP_FAILED = "SETUP_FAILED"
    EXPECTED_REJECTION_MISMATCH = "EXPECTED_REJECTION_MISMATCH"
    UNEXPECTED_REJECTION = "UNEXPECTED_REJECTION"
    HARD_CONSTRAINT_ESCAPE = "HARD_CONSTRAINT_ESCAPE"
    SAFETY_ESCAPE = "SAFETY_ESCAPE"
    NON_DETERMINISTIC_OUTPUT = "NON_DETERMINISTIC_OUTPUT"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"
    UNEXPECTED_SIDE_EFFECT = "UNEXPECTED_SIDE_EFFECT"
    DUPLICATE_CALENDAR_EVENT = "DUPLICATE_CALENDAR_EVENT"
    CAS_VIOLATION = "CAS_VIOLATION"
    IMMUTABILITY_VIOLATION = "IMMUTABILITY_VIOLATION"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CROSS_USER_LEAK = "CROSS_USER_LEAK"
    PENDING_MEMORY_LEAK = "PENDING_MEMORY_LEAK"
    EXPIRED_MEMORY_LEAK = "EXPIRED_MEMORY_LEAK"
    DELETED_MEMORY_LEAK = "DELETED_MEMORY_LEAK"
    IDEMPOTENCY_FAILURE = "IDEMPOTENCY_FAILURE"
    CONFLICT_NOT_DETECTED = "CONFLICT_NOT_DETECTED"
    CONTEXT_ORDER_MISMATCH = "CONTEXT_ORDER_MISMATCH"
    NO_MEMORY_DEGRADATION_FAILED = "NO_MEMORY_DEGRADATION_FAILED"


class AblationMode(StrEnum):
    FULL = "FULL"
    NO_MEMORY_CONTEXT = "NO_MEMORY_CONTEXT"
    DETERMINISTIC_SESSION_FALLBACK = "DETERMINISTIC_SESSION_FALLBACK"
    MANUAL_ONLY_CALENDAR = "MANUAL_ONLY_CALENDAR"
    NO_RECOVERY_ADJUSTMENT = "NO_RECOVERY_ADJUSTMENT"
    TEMPLATE_ONLY = "TEMPLATE_ONLY"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvaluationCaseBase(FrozenModel):
    case_id: str = Field(pattern=r"^(PLAN|MEM)-[0-9]{3}$")
    dataset_version: str = Field(min_length=1, max_length=40)
    schema_version: str = Field(min_length=1, max_length=40)
    category: str = Field(min_length=1, max_length=64)
    description_code: str = Field(pattern=r"^[A-Z0-9_]+$")
    seed: int = Field(ge=0)
    timezone: str = Field(min_length=1, max_length=64)
    expected_outcome: Literal["SUCCEEDED", "REJECTED"]
    expected_invariants: tuple[str, ...] = Field(min_length=1)
    expected_rejection_codes: tuple[str, ...] = ()
    tags: tuple[str, ...] = Field(min_length=1)

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def rejection_contract_is_consistent(self) -> EvaluationCaseBase:
        if self.expected_outcome == "REJECTED" and not self.expected_rejection_codes:
            raise ValueError("rejected cases require expected_rejection_codes")
        if self.expected_outcome == "SUCCEEDED" and self.expected_rejection_codes:
            raise ValueError("successful cases cannot expect rejection codes")
        return self


class PlanConstraintFixture(FrozenModel):
    constraint_type: str
    value_code: str
    priority: int = Field(default=100, ge=0)
    is_hard: bool = True


class PlanCaseInput(FrozenModel):
    weekly_frequency: int
    max_session_minutes: int
    experience_level: str
    primary_goal: str
    scope_confirmed: bool = True
    location: str = "HOME"
    slot_count: int = Field(default=5, ge=0, le=7)
    slot_duration_minutes: int = Field(default=60, ge=1, le=120)
    schedule_variant: Literal["VALID", "OVERLAP"] = "VALID"
    catalog_mode: Literal["STANDARD", "DISABLED_ONLY", "EMPTY"] = "STANDARD"
    constraints: tuple[PlanConstraintFixture, ...] = ()
    calendar_mode: Literal["VERIFIED", "MANUAL_ONLY"] = "VERIFIED"
    history_mode: Literal["NONE", "COMPLETED", "CHECKED_IN"] = "NONE"
    expected_plan_version: int = Field(default=1, ge=1)


class PlanEvaluationCase(EvaluationCaseBase):
    input: PlanCaseInput


class MemoryCaseInput(FrozenModel):
    scenario: Literal[
        "ACTIVE_RECALL",
        "PENDING_REVIEW",
        "REJECTED",
        "DELETED",
        "EXPIRED",
        "TTL_BOUNDARY",
        "DUPLICATE_CANDIDATE",
        "CONFLICT_CANDIDATE",
        "EVIDENCE",
        "NO_MEMORY",
        "CROSS_USER",
        "CONTEXT_ORDER",
    ]
    memory_type: str = "PREFERRED_LOCATION"
    valid_until_offset_seconds: int | None = 86400
    expected_recall_count: int = Field(default=0, ge=0, le=3)
    query_failure_count: int = Field(default=0, ge=0, le=2)
    context_limit: int = Field(default=8, ge=1, le=20)


class MemoryEvaluationCase(EvaluationCaseBase):
    input: MemoryCaseInput


class EvaluationCaseResult(FrozenModel):
    case_id: str
    dataset: Literal["plan", "memory"]
    category: str
    status: EvaluationCaseStatus
    duration_ms: float = Field(ge=0)
    seed: int
    ablation: AblationMode
    output_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    violations: tuple[str, ...] = ()
    failure_category: EvaluationFailureCategory | None = None
    error_code: str | None = None
    safe_description_code: str | None = None
    metric_values: dict[str, float]
    trace_reference: str
    deterministic_repetitions: int = Field(ge=1)


class GateCheck(FrozenModel):
    gate: str
    passed: bool
    actual: float
    threshold: float
    comparison: Literal["min", "max"]
    zero_tolerance: bool = False


class EvaluationRunReport(FrozenModel):
    dataset: Literal["plan", "memory", "ablation"]
    dataset_version: str
    config_version: str
    schema_version: str
    seed: int
    timezone: str
    ablation: AblationMode | None
    started_at: datetime
    completed_at: datetime
    dataset_sha256: str
    config_sha256: str
    total: int
    passed: int
    failed: int
    skipped: int
    timed_out: int
    internal_error: int
    metrics: dict[str, float]
    gates: tuple[GateCheck, ...]
    gate_passed: bool
    results: tuple[EvaluationCaseResult, ...]


class DatasetManifestEntry(FrozenModel):
    dataset_name: str
    path: str
    dataset_version: str
    schema_version: str
    case_count: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    generator_version: str


class DatasetManifest(FrozenModel):
    manifest_version: str
    datasets: tuple[DatasetManifestEntry, ...]


class AblationSpec(FrozenModel):
    mode: AblationMode
    memory_context_enabled: bool
    session_designer_enabled: bool
    calendar_availability_enabled: bool
    recovery_adjustment_enabled: bool
    model_enabled: bool
    safety_enabled: Literal[True]
    permission_gate_enabled: Literal[True]
    confirmation_gate_enabled: Literal[True]
    idempotency_enabled: Literal[True]
    memory_isolation_enabled: Literal[True]
    calendar_operation_key_enabled: Literal[True]
    plan_cas_enabled: Literal[True]


class AblationComparison(FrozenModel):
    mode: AblationMode
    plan_metrics: dict[str, float]
    memory_metrics: dict[str, float]
    average_evaluation_latency_ms: float
    zero_tolerance_passed: bool


JsonValue = dict[str, Any]
