"""Frozen Candidate Set construction and privacy-safe fingerprints."""

import hashlib
import json
from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.common import utc_now
from app.domain.context.models import ContextSnapshotReference
from app.domain.exercises.models import Exercise
from app.domain.session_design.models import CandidateSlot, ExerciseCandidateSet
from app.domain.session_design.templates import SessionTemplate


def catalog_version(catalog: Sequence[Exercise]) -> str:
    payload = [
        (item.id, item.version, item.status.value)
        for item in sorted(catalog, key=lambda item: item.id)
    ]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode()
    ).hexdigest()


def build_candidate_set(
    *,
    user_id: UUID,
    request_fingerprint: str,
    template: SessionTemplate,
    catalog: Sequence[Exercise],
    context: ContextSnapshotReference,
    slots: tuple[CandidateSlot, ...],
) -> ExerciseCandidateSet:
    payload = {
        "user_id": str(user_id),
        "request": request_fingerprint,
        "template": template.id.value,
        "template_version": template.version,
        "catalog_version": catalog_version(catalog),
        "context": context.context_fingerprint,
        "slots": [(slot.slot_id, slot.role.value, slot.exercise_ids) for slot in slots],
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ExerciseCandidateSet(
        id=uuid5(NAMESPACE_URL, f"fitweek:session-candidates:{user_id}:{fingerprint}"),
        user_id=user_id,
        request_fingerprint=request_fingerprint,
        fingerprint=fingerprint,
        template_id=template.id,
        template_version=template.version,
        catalog_version=str(payload["catalog_version"]),
        context_snapshot_reference_id=context.id,
        context_fingerprint=context.context_fingerprint,
        slots=slots,
        created_at=utc_now(),
    )
