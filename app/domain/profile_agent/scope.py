"""Deterministic pre-model scope classification for the MVP product boundary."""

import re
from dataclasses import dataclass

from app.domain.profile_agent.models import ScopeStatus

_OUT_OF_SCOPE_PATTERNS = (
    r"胸痛",
    r"晕厥",
    r"术后(?:恢复|康复)?",
    r"骨折(?:恢复|康复)?",
    r"孕期(?:训练|运动)?",
    r"心脏(?:疾病)?(?:运动)?处方",
    r"糖尿病(?:运动)?处方",
    r"慢性病(?:治疗|运动处方)",
    r"伤病康复",
    r"医学诊断",
    r"处方调整",
    r"chest\s+pain",
    r"syncope|fainting",
    r"post[- ]?operative|post[- ]?surgery",
    r"fracture\s+(?:recovery|rehabilitation)",
    r"pregnan(?:t|cy).*(?:training|exercise)",
    r"cardiac.*exercise\s+prescription",
    r"diabet(?:es|ic).*exercise\s+prescription",
    r"chronic\s+disease\s+treatment",
    r"injury\s+rehabilitation",
    r"medical\s+diagnosis",
    r"prescription\s+adjustment",
)

_NEEDS_REVIEW_PATTERNS = (
    r"膝盖不舒服",
    r"运动时头晕",
    r"可能受伤",
    r"knee\s+discomfort",
    r"dizz(?:y|iness)\s+(?:during|when).*(?:exercise|training)",
    r"possible\s+injury|might\s+be\s+injured",
)


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    status: ScopeStatus
    reason_code: str | None


class DeterministicScopeGuard:
    """Classify explicit medical scope before any provider can be called."""

    def classify(self, user_message: str) -> ScopeDecision:
        normalized = re.sub(r"\s+", " ", user_message.strip().casefold())
        if not normalized:
            return ScopeDecision(ScopeStatus.NEEDS_REVIEW, "EMPTY_REQUEST")
        if any(
            re.search(pattern, normalized, re.IGNORECASE)
            for pattern in _OUT_OF_SCOPE_PATTERNS
        ):
            return ScopeDecision(ScopeStatus.OUT_OF_SCOPE, "MEDICAL_SCOPE_EXCLUDED")
        if any(
            re.search(pattern, normalized, re.IGNORECASE)
            for pattern in _NEEDS_REVIEW_PATTERNS
        ):
            return ScopeDecision(ScopeStatus.NEEDS_REVIEW, "AMBIGUOUS_HEALTH_RISK")
        return ScopeDecision(ScopeStatus.SUPPORTED, None)
