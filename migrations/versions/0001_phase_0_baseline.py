"""Establish the empty phase 0 baseline compatible with MySQL.

Revision ID: 0001_phase_0_baseline
Revises: None
"""

from collections.abc import Sequence

revision: str = "0001_phase_0_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Advance to the MySQL-compatible baseline without business tables."""


def downgrade() -> None:
    """Return to base; the baseline owns no business tables."""
