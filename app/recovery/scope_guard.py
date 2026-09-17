"""Deterministic bilingual scope guard executed before Context or providers."""

from dataclasses import dataclass

from app.domain.recovery.enums import RecoveryScopeStatus


@dataclass(frozen=True, slots=True)
class RecoveryScopeResult:
    status: RecoveryScopeStatus
    reason_code: str
    safe_message: str


class RecoveryScopeGuard:
    _OUT_OF_SCOPE = (
        "chest pain",
        "fainting",
        "fainted",
        "syncope",
        "severe dizziness",
        "difficulty breathing",
        "shortness of breath",
        "suspected fracture",
        "fracture",
        "acute injury",
        "post-operative",
        "postoperative rehabilitation",
        "pregnancy-specific",
        "medical diagnosis",
        "medication adjustment",
        "physical therapy prescription",
        "胸痛",
        "胸口痛",
        "晕厥",
        "昏厥",
        "严重头晕",
        "呼吸困难",
        "疑似骨折",
        "骨折",
        "急性受伤",
        "术后康复",
        "孕期运动处方",
        "医疗诊断",
        "药物调整",
        "物理治疗处方",
    )
    _NEEDS_REVIEW = (
        "不舒服",
        "膝盖有点异常",
        "经常头晕",
        "feel unwell",
        "knee feels unusual",
        "often dizzy",
        "persistent discomfort",
    )

    def evaluate(self, user_request: str) -> RecoveryScopeResult:
        normalized = " ".join(user_request.casefold().split())
        if any(value in normalized for value in self._OUT_OF_SCOPE):
            return RecoveryScopeResult(
                status=RecoveryScopeStatus.OUT_OF_SCOPE,
                reason_code="RECOVERY_SCOPE_OUT_OF_SCOPE",
                safe_message=(
                    "This request is outside FitWeek's non-medical scope. "
                    "Please stop the activity and seek appropriate professional help."
                ),
            )
        if any(value in normalized for value in self._NEEDS_REVIEW):
            return RecoveryScopeResult(
                status=RecoveryScopeStatus.NEEDS_REVIEW,
                reason_code="RECOVERY_SCOPE_NEEDS_REVIEW",
                safe_message=(
                    "No training adjustment was proposed because the request needs "
                    "clarification outside this non-medical workflow."
                ),
            )
        return RecoveryScopeResult(
            status=RecoveryScopeStatus.SUPPORTED,
            reason_code="RECOVERY_SCOPE_SUPPORTED",
            safe_message="The request is within the supported scheduling scope.",
        )
