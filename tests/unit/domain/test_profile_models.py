"""Intrinsic invariants for profiles and structured constraints."""

from dataclasses import replace
from datetime import datetime

import pytest

from app.domain.common import DomainValidationError
from app.domain.profiles.models import ConstraintType
from tests.factories import TEST_NOW, make_constraint, make_profile

pytestmark = pytest.mark.phase_1a


@pytest.mark.parametrize("weekly_frequency", [1, 6, True])
def test_profile_rejects_weekly_frequency_outside_mvp_range(
    weekly_frequency: int,
) -> None:
    with pytest.raises(
        DomainValidationError, match="weekly_frequency must be between 2 and 5"
    ):
        make_profile(weekly_frequency=weekly_frequency)


@pytest.mark.parametrize("max_session_minutes", [14, 61, True])
def test_profile_rejects_session_limit_outside_mvp_range(
    max_session_minutes: int,
) -> None:
    with pytest.raises(
        DomainValidationError,
        match="max_session_minutes must be between 15 and 60",
    ):
        make_profile(max_session_minutes=max_session_minutes)


def test_profile_requires_utc_timestamps() -> None:
    profile = make_profile()

    with pytest.raises(DomainValidationError, match="timezone-aware"):
        replace(profile, updated_at=datetime(2026, 7, 17))


@pytest.mark.parametrize("version", [0, -1, True])
def test_profile_requires_positive_version(version: int) -> None:
    with pytest.raises(DomainValidationError, match="positive integer"):
        make_profile(version=version)


def test_constraint_rejects_blank_value_and_naive_expiration() -> None:
    profile = make_profile()
    constraint = make_constraint(
        profile.id,
        ConstraintType.AVAILABLE_EQUIPMENT,
        "resistance_band",
    )

    with pytest.raises(DomainValidationError, match="must not be blank"):
        replace(constraint, constraint_value="   ")

    with pytest.raises(DomainValidationError, match="timezone-aware"):
        replace(constraint, valid_until=TEST_NOW.replace(tzinfo=None))
