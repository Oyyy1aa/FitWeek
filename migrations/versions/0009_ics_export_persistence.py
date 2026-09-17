"""Persist immutable ICS export bytes and metadata.

Revision ID: 0009_ics_export_persistence
Revises: 0008_orchestration_status_capacity
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0009_ics_export_persistence"
down_revision = "0008_orchestration_status_capacity"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("ics_export"):
        return
    op.create_table(
        "ics_export",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("client_request_id", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("root_plan_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content", mysql.LONGBLOB(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.UniqueConstraint(
            "user_id",
            "client_request_id",
            name="uq_ics_export_user_request",
        ),
    )
    op.create_index(
        "ix_ics_export_user_plan_revision",
        "ics_export",
        ["user_id", "root_plan_id", "revision"],
    )


def downgrade() -> None:
    if context.is_offline_mode() or _has_table("ics_export"):
        op.drop_index("ix_ics_export_user_plan_revision", table_name="ics_export")
        op.drop_table("ics_export")
