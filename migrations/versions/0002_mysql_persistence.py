"""Create FitWeek MySQL persistence tables.

Revision ID: 0002_mysql_persistence
Revises: 0001_phase_0_baseline
"""

from collections.abc import Sequence

from alembic import op

from app.persistence.mysql.models import Base

revision: str = "0002_mysql_persistence"
down_revision: str | None = "0001_phase_0_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the normalized MySQL source-of-truth tables."""

    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    """Remove only tables created by this revision, in dependency-safe order."""

    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
