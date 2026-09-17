"""Controlled Evidence summaries that never persist source free text."""

from app.domain.memory.enums import MemoryEvidenceType, MemoryType


def evidence_summary(evidence_type: MemoryEvidenceType, memory_type: MemoryType) -> str:
    return f"{evidence_type.value} confirmed a structured {memory_type.value} value."
