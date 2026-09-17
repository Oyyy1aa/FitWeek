"""Persist frozen Session Design candidates, Drafts, traces, and applications.

Revision ID: 0005_session_design_draft
Revises: 0004_profile_draft
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0005_session_design_draft"
down_revision = "0004_profile_draft"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    if context.is_offline_mode():
        return False
    return any(
        item["name"] == column for item in sa.inspect(op.get_bind()).get_columns(table)
    )


def upgrade() -> None:
    # A clean upgrade runs historical 0002 against current metadata. Guard each
    # operation so that the same linear history also upgrades a real 0004 DB.
    if not _has_column("workout_session", "logical_session_id"):
        op.add_column(
            "workout_session",
            sa.Column("logical_session_id", sa.String(length=36), nullable=True),
        )
        op.execute(
            sa.text(
                "UPDATE workout_session "
                "SET logical_session_id = id "
                "WHERE logical_session_id IS NULL"
            )
        )
        op.alter_column(
            "workout_session",
            "logical_session_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        op.create_unique_constraint(
            "uq_workout_session_plan_logical",
            "workout_session",
            ["plan_id", "logical_session_id"],
        )
        op.create_index(
            "ix_workout_session_logical",
            "workout_session",
            ["logical_session_id"],
        )

    if not _has_table("session_design_candidate_set"):
        op.create_table(
            "session_design_candidate_set",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("fingerprint", sa.String(length=64), nullable=False),
            sa.Column("template_id", sa.String(length=64), nullable=False),
            sa.Column("template_version", sa.String(length=64), nullable=False),
            sa.Column("catalog_version", sa.String(length=64), nullable=False),
            sa.Column("context_snapshot_id", sa.String(length=36), nullable=False),
            sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
            sa.ForeignKeyConstraint(
                ["context_snapshot_id"],
                ["context_snapshot.id"],
            ),
            sa.UniqueConstraint(
                "user_id",
                "request_fingerprint",
                "fingerprint",
                name="uq_session_design_candidate_fingerprint",
            ),
        )
        op.create_index(
            "ix_session_design_candidate_set_user_id",
            "session_design_candidate_set",
            ["user_id"],
        )
        op.create_index(
            "ix_session_design_candidate_context",
            "session_design_candidate_set",
            ["context_snapshot_id"],
        )

    if not _has_table("session_design_candidate_slot"):
        op.create_table(
            "session_design_candidate_slot",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("candidate_set_id", sa.String(length=36), nullable=False),
            sa.Column("slot_id", sa.String(length=80), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=False),
            sa.Column("exercise_ids", sa.JSON(), nullable=False),
            sa.Column("candidate_order", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["candidate_set_id"],
                ["session_design_candidate_set.id"],
            ),
            sa.UniqueConstraint(
                "candidate_set_id",
                "slot_id",
                name="uq_session_design_candidate_slot",
            ),
            sa.UniqueConstraint(
                "candidate_set_id",
                "candidate_order",
                name="uq_session_design_candidate_order",
            ),
        )
        op.create_index(
            "ix_session_design_candidate_slot_set",
            "session_design_candidate_slot",
            ["candidate_set_id"],
        )

    if not _has_table("session_design_draft"):
        op.create_table(
            "session_design_draft",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("request_id", sa.String(length=36), nullable=False),
            sa.Column("client_request_id", sa.String(length=128), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column(
                "request_payload_fingerprint",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column("candidate_set_id", sa.String(length=36), nullable=False),
            sa.Column(
                "candidate_set_fingerprint",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column("context_snapshot_id", sa.String(length=36), nullable=False),
            sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("context_degraded_mode", sa.String(length=32), nullable=False),
            sa.Column("template_id", sa.String(length=64), nullable=False),
            sa.Column("template_version", sa.String(length=64), nullable=False),
            sa.Column("catalog_version", sa.String(length=64), nullable=False),
            sa.Column("session_type", sa.String(length=32), nullable=False),
            sa.Column("target_date", sa.Date(), nullable=False),
            sa.Column("target_duration_minutes", sa.Integer(), nullable=False),
            sa.Column("location", sa.String(length=32), nullable=False),
            sa.Column("goal", sa.String(length=64), nullable=False),
            sa.Column("exercises", sa.JSON(), nullable=False),
            sa.Column("exercise_roles", sa.JSON(), nullable=False),
            sa.Column("duration", sa.JSON(), nullable=False),
            sa.Column("safety_validation", sa.JSON(), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("prompt_version", sa.String(length=128), nullable=False),
            sa.Column("provider_summary", sa.String(length=240), nullable=False),
            sa.Column("fallback_used", sa.Boolean(), nullable=False),
            sa.Column("explanation_summary", sa.String(length=600), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("reviewed_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("applied_root_plan_id", sa.String(length=36), nullable=True),
            sa.Column("applied_revision", sa.Integer(), nullable=True),
            sa.Column("applied_session_id", sa.String(length=36), nullable=True),
            sa.Column("application_result_id", sa.String(length=36), nullable=True),
            sa.Column("applied_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
            sa.ForeignKeyConstraint(
                ["candidate_set_id"],
                ["session_design_candidate_set.id"],
            ),
            sa.ForeignKeyConstraint(
                ["context_snapshot_id"],
                ["context_snapshot.id"],
            ),
            sa.ForeignKeyConstraint(
                ["applied_root_plan_id"],
                ["weekly_plan.id"],
            ),
            sa.UniqueConstraint(
                "user_id",
                "client_request_id",
                name="uq_session_design_draft_request",
            ),
        )
        op.create_index(
            "ix_session_design_draft_user_status",
            "session_design_draft",
            ["user_id", "status"],
        )
        op.create_index(
            "ix_session_design_draft_expires_at",
            "session_design_draft",
            ["expires_at"],
        )
        op.create_index(
            "ix_session_design_draft_candidate_set",
            "session_design_draft",
            ["candidate_set_id"],
        )
        op.create_index(
            "ix_session_design_draft_context",
            "session_design_draft",
            ["context_snapshot_id"],
        )

    if not _has_table("session_design_trace"):
        op.create_table(
            "session_design_trace",
            sa.Column("draft_id", sa.String(length=36), primary_key=True),
            sa.Column("request_id", sa.String(length=36), nullable=False),
            sa.Column("candidate_set_id", sa.String(length=36), nullable=False),
            sa.Column(
                "candidate_set_fingerprint",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column("context_snapshot_id", sa.String(length=36), nullable=False),
            sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("prompt_version", sa.String(length=128), nullable=False),
            sa.Column("template_id", sa.String(length=64), nullable=False),
            sa.Column("template_version", sa.String(length=64), nullable=False),
            sa.Column("provider_summary", sa.String(length=240), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("fallback_used", sa.Boolean(), nullable=False),
            sa.Column("validation_error_code", sa.String(length=128), nullable=True),
            sa.Column("model_trace_ids", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["draft_id"], ["session_design_draft.id"]),
            sa.ForeignKeyConstraint(
                ["candidate_set_id"],
                ["session_design_candidate_set.id"],
            ),
            sa.ForeignKeyConstraint(
                ["context_snapshot_id"],
                ["context_snapshot.id"],
            ),
        )

    if not _has_table("session_design_application_result"):
        op.create_table(
            "session_design_application_result",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("client_request_id", sa.String(length=128), nullable=False),
            sa.Column(
                "application_fingerprint",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column("draft_id", sa.String(length=36), nullable=False),
            sa.Column("root_plan_id", sa.String(length=36), nullable=False),
            sa.Column("source_revision", sa.Integer(), nullable=False),
            sa.Column("created_revision", sa.Integer(), nullable=False),
            sa.Column("target_session_id", sa.String(length=36), nullable=False),
            sa.Column("previous_plan_version", sa.Integer(), nullable=False),
            sa.Column("resulting_plan_version", sa.Integer(), nullable=False),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
            sa.ForeignKeyConstraint(["draft_id"], ["session_design_draft.id"]),
            sa.UniqueConstraint(
                "user_id",
                "client_request_id",
                name="uq_session_design_application_request",
            ),
            sa.UniqueConstraint(
                "draft_id",
                name="uq_session_design_application_draft",
            ),
        )
        op.create_index(
            "ix_session_design_application_user_id",
            "session_design_application_result",
            ["user_id"],
        )
        op.create_index(
            "ix_session_design_application_root_revision",
            "session_design_application_result",
            ["root_plan_id", "created_revision"],
        )


def downgrade() -> None:
    op.drop_table("session_design_application_result")
    op.drop_table("session_design_trace")
    op.drop_table("session_design_draft")
    op.drop_table("session_design_candidate_slot")
    op.drop_table("session_design_candidate_set")
    op.drop_index("ix_workout_session_logical", table_name="workout_session")
    op.drop_constraint(
        "uq_workout_session_plan_logical",
        "workout_session",
        type_="unique",
    )
    op.drop_column("workout_session", "logical_session_id")
