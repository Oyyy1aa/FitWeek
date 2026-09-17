"""Persist Profile Agent drafts and their review applications.

Revision ID: 0004_profile_draft
Revises: 0003_memory_ctx
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0004_profile_draft"
down_revision = "0003_memory_ctx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical revision 0002 creates the then-current SQLAlchemy metadata.
    # A database upgraded from base with today's models therefore already has
    # this table, while a real database currently at 0003 does not. Keep 0004
    # safe for both paths without changing the applied historical migration.
    if not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table(
        "profile_draft"
    ):
        return
    op.create_table(
        "profile_draft",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("client_request_id", sa.String(length=128), nullable=False),
        sa.Column("request_payload_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("output", sa.JSON(), nullable=False),
        sa.Column("prompt_version", sa.String(length=128), nullable=False),
        sa.Column("provider_summary", sa.String(length=240), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("fallback_type", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("context_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("context_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("context_contract_version", sa.String(length=64), nullable=True),
        sa.Column("context_policy_version", sa.String(length=64), nullable=True),
        sa.Column("context_degraded_mode", sa.String(length=32), nullable=False),
        sa.Column("context_included_memory_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("applied_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("rejected_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("applied_profile_id", sa.String(length=36), nullable=True),
        sa.Column("apply_request_id", sa.String(length=128), nullable=True),
        sa.Column("apply_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("apply_result", sa.JSON(), nullable=True),
        sa.Column("reject_request_id", sa.String(length=128), nullable=True),
        sa.Column("reject_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.ForeignKeyConstraint(["context_snapshot_id"], ["context_snapshot.id"]),
        sa.ForeignKeyConstraint(["applied_profile_id"], ["fitness_profile.id"]),
        sa.UniqueConstraint(
            "user_id", "client_request_id", name="uq_profile_draft_request"
        ),
        sa.UniqueConstraint(
            "user_id", "apply_request_id", name="uq_profile_draft_apply"
        ),
        sa.UniqueConstraint(
            "user_id", "reject_request_id", name="uq_profile_draft_reject"
        ),
    )
    op.create_index(
        "ix_profile_draft_user_status", "profile_draft", ["user_id", "status"]
    )
    op.create_index("ix_profile_draft_expires_at", "profile_draft", ["expires_at"])
    op.create_index(
        "ix_profile_draft_context_snapshot", "profile_draft", ["context_snapshot_id"]
    )


def downgrade() -> None:
    # MySQL uses the context index to enforce its foreign key. Dropping the
    # table removes its indexes atomically without violating that dependency.
    op.drop_table("profile_draft")
