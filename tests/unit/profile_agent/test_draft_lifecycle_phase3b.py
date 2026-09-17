"""Profile Draft lifecycle tests for Phase 3B."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.domain.profile_agent.models import ProfileDraftStatus
from tests.phase3b_helpers import NOW, make_draft

pytestmark = pytest.mark.phase_3b


def test_pending_draft_transitions_to_applied_with_required_audit_fields() -> None:
    draft = make_draft()

    applied = draft.mark_applied(
        applied_at=NOW + timedelta(minutes=1),
        profile_id=uuid4(),
        apply_request_id="apply-1",
    )

    assert applied.status is ProfileDraftStatus.APPLIED
    assert applied.version == 2
    assert applied.applied_at is not None
    assert applied.applied_profile_id is not None
    assert applied.apply_request_id == "apply-1"
    with pytest.raises(ValueError, match="pending"):
        applied.mark_rejected(rejected_at=NOW + timedelta(minutes=2))


def test_pending_draft_transitions_to_rejected_and_is_terminal() -> None:
    rejected = make_draft().mark_rejected(rejected_at=NOW + timedelta(minutes=1))

    assert rejected.status is ProfileDraftStatus.REJECTED
    assert rejected.version == 2
    assert rejected.rejected_at is not None
    with pytest.raises(ValueError, match="pending"):
        rejected.mark_applied(
            applied_at=NOW + timedelta(minutes=2),
            profile_id=uuid4(),
            apply_request_id="apply-1",
        )


def test_expired_draft_is_terminal_and_preserves_no_apply_metadata() -> None:
    expired = make_draft().mark_expired()

    assert expired.status is ProfileDraftStatus.EXPIRED
    assert expired.version == 2
    assert expired.mark_expired() is expired
    with pytest.raises(ValueError, match="pending"):
        expired.mark_rejected(rejected_at=NOW + timedelta(minutes=1))


def test_draft_version_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        replace(make_draft(), version=0)
