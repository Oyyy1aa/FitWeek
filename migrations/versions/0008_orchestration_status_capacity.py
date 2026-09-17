"""Widen durable Run-status columns for all existing orchestration states.

Revision ID: 0008_orchestration_status_capacity
Revises: 0007_phase2b_orchestration_persistence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0008_orchestration_status_capacity"
down_revision = "0007_phase2b_orchestration_persistence"
branch_labels = None
depends_on = None


def _assert_downgrade_is_lossless() -> None:
    """Refuse before DDL when persisted values cannot fit the prior contract."""

    if context.is_offline_mode():
        return
    bind = op.get_bind()
    too_long = bind.scalar(
        sa.text(
            "SELECT EXISTS("
            "SELECT 1 FROM planning_run WHERE CHAR_LENGTH(status) > 32 "
            "UNION ALL "
            "SELECT 1 FROM planning_checkpoint "
            "WHERE CHAR_LENGTH(run_status_after) > 32"
            ")"
        )
    )
    if too_long:
        raise RuntimeError(
            "Refusing lossy downgrade: persisted Run status exceeds VARCHAR(32)."
        )


def upgrade() -> None:
    op.alter_column(
        "planning_run",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "planning_checkpoint",
        "run_status_after",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    _assert_downgrade_is_lossless()
    op.alter_column(
        "planning_checkpoint",
        "run_status_after",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.alter_column(
        "planning_run",
        "status",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
