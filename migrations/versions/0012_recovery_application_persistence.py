"""Persist atomic Recovery application results and receipts.

Revision ID: 0012_recovery_application_persistence
Revises: 0011_recovery_draft_persistence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0012_recovery_application_persistence"
down_revision = "0011_recovery_draft_persistence"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("recovery_application_result"):
        return

    time = mysql.DATETIME(fsp=6)
    op.create_table(
        "recovery_application_result",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("client_request_id", sa.String(128), nullable=False),
        sa.Column("application_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("recovery_draft_id", sa.String(36), nullable=False),
        sa.Column("root_plan_id", sa.String(36), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("created_revision", sa.Integer()),
        sa.Column("applied_action_candidate_ids", sa.JSON(), nullable=False),
        sa.Column("session_design_draft_ids", sa.JSON(), nullable=False),
        sa.Column("schedule_draft_ids", sa.JSON(), nullable=False),
        sa.Column("affected_session_ids", sa.JSON(), nullable=False),
        sa.Column("removed_session_ids", sa.JSON(), nullable=False),
        sa.Column("preserved_session_ids", sa.JSON(), nullable=False),
        sa.Column("immutable_session_ids", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.ForeignKeyConstraint(["recovery_draft_id"], ["recovery_draft.id"]),
        sa.UniqueConstraint(
            "user_id", "client_request_id", name="uq_recovery_application_request"
        ),
        sa.UniqueConstraint("recovery_draft_id", name="uq_recovery_application_draft"),
    )
    op.create_index(
        "ix_recovery_application_user_result",
        "recovery_application_result",
        ["user_id", "id"],
    )
    op.create_index(
        "ix_recovery_application_user_draft",
        "recovery_application_result",
        ["user_id", "recovery_draft_id"],
    )
    op.create_table(
        "recovery_session_design_subdraft_binding",
        sa.Column("recovery_draft_id", sa.String(36), nullable=False),
        sa.Column("candidate_id", sa.String(36), nullable=False),
        sa.Column("child_draft_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.ForeignKeyConstraint(["recovery_draft_id"], ["recovery_draft.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["recovery_action_candidate.id"]),
        sa.ForeignKeyConstraint(["child_draft_id"], ["session_design_draft.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.PrimaryKeyConstraint("recovery_draft_id", "candidate_id"),
    )
    op.create_index(
        "ix_recovery_session_design_binding_lookup",
        "recovery_session_design_subdraft_binding",
        ["recovery_draft_id", "candidate_id"],
    )
    op.create_table(
        "recovery_schedule_subdraft_binding",
        sa.Column("recovery_draft_id", sa.String(36), nullable=False),
        sa.Column("candidate_id", sa.String(36), nullable=False),
        sa.Column("child_draft_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.ForeignKeyConstraint(["recovery_draft_id"], ["recovery_draft.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["recovery_action_candidate.id"]),
        sa.ForeignKeyConstraint(["child_draft_id"], ["schedule_draft.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.PrimaryKeyConstraint("recovery_draft_id", "candidate_id"),
    )
    op.create_index(
        "ix_recovery_schedule_binding_lookup",
        "recovery_schedule_subdraft_binding",
        ["recovery_draft_id", "candidate_id"],
    )
    op.create_table(
        "recovery_memory_proposal_import",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("draft_id", sa.String(36), nullable=False),
        sa.Column("client_request_id", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("proposal_ids", sa.JSON(), nullable=False),
        sa.Column("memory_candidate_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.ForeignKeyConstraint(["draft_id"], ["recovery_draft.id"]),
        sa.UniqueConstraint(
            "user_id", "client_request_id", name="uq_recovery_memory_import_request"
        ),
    )
    op.create_index(
        "ix_recovery_memory_import_user_draft",
        "recovery_memory_proposal_import",
        ["user_id", "draft_id"],
    )


def downgrade() -> None:
    if context.is_offline_mode() or _has_table("recovery_application_result"):
        op.drop_table("recovery_memory_proposal_import")
        op.drop_table("recovery_schedule_subdraft_binding")
        op.drop_table("recovery_session_design_subdraft_binding")
        op.drop_table("recovery_application_result")
