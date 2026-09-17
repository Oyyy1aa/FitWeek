"""Persist frozen Schedule inputs, Drafts, traces, and applications.

Revision ID: 0006_schedule_draft
Revises: 0005_session_design_draft
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0006_schedule_draft"
down_revision = "0005_session_design_draft"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    # Historical 0002 creates current metadata on a clean database. Online
    # guards preserve both that clean path and a real 0005 -> 0006 upgrade.
    if not _has_table("schedule_busy_snapshot"):
        op.create_table(
            "schedule_busy_snapshot",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("timezone", sa.String(length=100), nullable=False),
            sa.Column("range_start_utc", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("range_end_utc", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("mode", sa.String(length=32), nullable=False),
            sa.Column("verification_status", sa.String(length=32), nullable=False),
            sa.Column("fingerprint", sa.String(length=64), nullable=False),
            sa.Column("provider_summary", sa.String(length=240), nullable=False),
            sa.Column("provider_name", sa.String(length=120), nullable=False),
            sa.Column("provider_version", sa.String(length=64), nullable=False),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
            sa.UniqueConstraint(
                "user_id",
                "fingerprint",
                name="uq_schedule_busy_snapshot_fingerprint",
            ),
        )
        op.create_index(
            "ix_schedule_busy_snapshot_user_created",
            "schedule_busy_snapshot",
            ["user_id", "created_at"],
        )

    if not _has_table("schedule_busy_interval"):
        op.create_table(
            "schedule_busy_interval",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("snapshot_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("starts_at_utc", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("ends_at_utc", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("interval_order", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["snapshot_id"],
                ["schedule_busy_snapshot.id"],
            ),
            sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
            sa.UniqueConstraint(
                "snapshot_id",
                "interval_order",
                name="uq_schedule_busy_interval_order",
            ),
        )
        op.create_index(
            "ix_schedule_busy_interval_user_start",
            "schedule_busy_interval",
            ["user_id", "starts_at_utc"],
        )

    if not _has_table("schedule_candidate_set"):
        op.create_table(
            "schedule_candidate_set",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("root_plan_id", sa.String(length=36), nullable=False),
            sa.Column("source_revision", sa.Integer(), nullable=False),
            sa.Column("busy_snapshot_id", sa.String(length=36), nullable=False),
            sa.Column("context_snapshot_id", sa.String(length=36), nullable=False),
            sa.Column("session_candidates", sa.JSON(), nullable=False),
            sa.Column("fingerprint", sa.String(length=64), nullable=False),
            sa.Column("policy_version", sa.String(length=64), nullable=False),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
            sa.ForeignKeyConstraint(
                ["busy_snapshot_id"],
                ["schedule_busy_snapshot.id"],
            ),
            sa.ForeignKeyConstraint(
                ["context_snapshot_id"],
                ["context_snapshot.id"],
            ),
            sa.UniqueConstraint(
                "user_id",
                "fingerprint",
                name="uq_schedule_candidate_fingerprint",
            ),
        )
        op.create_index(
            "ix_schedule_candidate_user_revision",
            "schedule_candidate_set",
            ["user_id", "root_plan_id", "source_revision"],
        )
        op.create_index(
            "ix_schedule_candidate_context",
            "schedule_candidate_set",
            ["context_snapshot_id"],
        )

    if not _has_table("schedule_availability_window"):
        op.create_table(
            "schedule_availability_window",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("candidate_set_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("starts_at_utc", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("ends_at_utc", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("location", sa.String(length=32), nullable=False),
            sa.Column("window_order", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["candidate_set_id"],
                ["schedule_candidate_set.id"],
            ),
            sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
            sa.PrimaryKeyConstraint("candidate_set_id", "id"),
            sa.UniqueConstraint(
                "candidate_set_id",
                "window_order",
                name="uq_schedule_availability_order",
            ),
        )
        op.create_index(
            "ix_schedule_availability_user_start",
            "schedule_availability_window",
            ["user_id", "starts_at_utc"],
        )

    if not _has_table("schedule_candidate_slot"):
        op.create_table(
            "schedule_candidate_slot",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("candidate_set_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("slot_id", sa.String(length=120), nullable=False),
            sa.Column("session_id", sa.String(length=36), nullable=False),
            sa.Column("starts_at_utc", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("ends_at_utc", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("location", sa.String(length=32), nullable=False),
            sa.Column("preference_score", sa.Integer(), nullable=False),
            sa.Column("timezone", sa.String(length=100), nullable=False),
            sa.Column("source_availability_id", sa.String(length=36), nullable=False),
            sa.Column("candidate_order", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["candidate_set_id"],
                ["schedule_candidate_set.id"],
            ),
            sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
            sa.PrimaryKeyConstraint("candidate_set_id", "id"),
            sa.UniqueConstraint(
                "candidate_set_id",
                "slot_id",
                name="uq_schedule_candidate_slot",
            ),
            sa.UniqueConstraint(
                "candidate_set_id",
                "candidate_order",
                name="uq_schedule_candidate_order",
            ),
        )
        op.create_index(
            "ix_schedule_candidate_slot_session_start",
            "schedule_candidate_slot",
            ["session_id", "starts_at_utc"],
        )

    if not _has_table("schedule_draft"):
        op.create_table(
            "schedule_draft",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("request_id", sa.String(length=36), nullable=False),
            sa.Column("client_request_id", sa.String(length=128), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column(
                "request_payload_fingerprint",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("root_plan_id", sa.String(length=36), nullable=False),
            sa.Column("source_revision", sa.Integer(), nullable=False),
            sa.Column("source_plan_version", sa.Integer(), nullable=False),
            sa.Column("timezone", sa.String(length=100), nullable=False),
            sa.Column("busy_snapshot_id", sa.String(length=36), nullable=False),
            sa.Column("candidate_set_id", sa.String(length=36), nullable=False),
            sa.Column(
                "candidate_set_fingerprint",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column("context_snapshot_id", sa.String(length=36), nullable=False),
            sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("context_degraded_mode", sa.String(length=32), nullable=False),
            sa.Column("assignments", sa.JSON(), nullable=False),
            sa.Column("unresolved", sa.JSON(), nullable=False),
            sa.Column("outcome", sa.String(length=32), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("prompt_version", sa.String(length=128), nullable=False),
            sa.Column("provider_summary", sa.String(length=240), nullable=False),
            sa.Column("fallback_used", sa.Boolean(), nullable=False),
            sa.Column(
                "calendar_verification_status",
                sa.String(length=32),
                nullable=False,
            ),
            sa.Column("explanation_summary", sa.String(length=600), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("reviewed_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("applied_root_plan_id", sa.String(length=36), nullable=True),
            sa.Column("applied_source_revision", sa.Integer(), nullable=True),
            sa.Column("applied_created_revision", sa.Integer(), nullable=True),
            sa.Column("application_result_id", sa.String(length=36), nullable=True),
            sa.Column("applied_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
            sa.ForeignKeyConstraint(
                ["busy_snapshot_id"],
                ["schedule_busy_snapshot.id"],
            ),
            sa.ForeignKeyConstraint(
                ["candidate_set_id"],
                ["schedule_candidate_set.id"],
            ),
            sa.ForeignKeyConstraint(
                ["context_snapshot_id"],
                ["context_snapshot.id"],
            ),
            sa.UniqueConstraint(
                "user_id",
                "client_request_id",
                name="uq_schedule_draft_request",
            ),
        )
        op.create_index(
            "ix_schedule_draft_user_status",
            "schedule_draft",
            ["user_id", "status"],
        )
        op.create_index(
            "ix_schedule_draft_expires_at",
            "schedule_draft",
            ["expires_at"],
        )
        op.create_index(
            "ix_schedule_draft_candidate",
            "schedule_draft",
            ["candidate_set_id"],
        )
        op.create_index(
            "ix_schedule_draft_context",
            "schedule_draft",
            ["context_snapshot_id"],
        )

    if not _has_table("schedule_trace"):
        op.create_table(
            "schedule_trace",
            sa.Column("draft_id", sa.String(length=36), primary_key=True),
            sa.Column("request_id", sa.String(length=36), nullable=False),
            sa.Column("busy_snapshot_id", sa.String(length=36), nullable=False),
            sa.Column("candidate_set_id", sa.String(length=36), nullable=False),
            sa.Column(
                "candidate_set_fingerprint",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column("context_snapshot_id", sa.String(length=36), nullable=False),
            sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("prompt_version", sa.String(length=128), nullable=False),
            sa.Column("provider_summary", sa.String(length=240), nullable=False),
            sa.Column("timezone", sa.String(length=100), nullable=False),
            sa.Column("provider_name", sa.String(length=120), nullable=False),
            sa.Column("provider_version", sa.String(length=64), nullable=False),
            sa.Column("attempt_no", sa.Integer(), nullable=False),
            sa.Column("outcome", sa.String(length=64), nullable=False),
            sa.Column("validation_error_code", sa.String(length=128), nullable=True),
            sa.Column("latency_ms", sa.Numeric(12, 3), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("fallback_used", sa.Boolean(), nullable=False),
            sa.Column("provider_attempts", sa.Integer(), nullable=False),
            sa.Column("calendar_mode", sa.String(length=32), nullable=False),
            sa.Column("calendar_attempts", sa.Integer(), nullable=False),
            sa.Column("model_trace_ids", sa.JSON(), nullable=False),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.ForeignKeyConstraint(["draft_id"], ["schedule_draft.id"]),
            sa.ForeignKeyConstraint(
                ["busy_snapshot_id"],
                ["schedule_busy_snapshot.id"],
            ),
            sa.ForeignKeyConstraint(
                ["candidate_set_id"],
                ["schedule_candidate_set.id"],
            ),
            sa.ForeignKeyConstraint(
                ["context_snapshot_id"],
                ["context_snapshot.id"],
            ),
        )

    if not _has_table("schedule_application_result"):
        op.create_table(
            "schedule_application_result",
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
            sa.Column("previous_plan_version", sa.Integer(), nullable=False),
            sa.Column("resulting_plan_version", sa.Integer(), nullable=False),
            sa.Column("changed_session_ids", sa.JSON(), nullable=False),
            sa.Column(
                "calendar_verification_status",
                sa.String(length=32),
                nullable=False,
            ),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
            sa.ForeignKeyConstraint(["draft_id"], ["schedule_draft.id"]),
            sa.UniqueConstraint(
                "user_id",
                "client_request_id",
                name="uq_schedule_application_request",
            ),
            sa.UniqueConstraint(
                "draft_id",
                name="uq_schedule_application_draft",
            ),
        )
        op.create_index(
            "ix_schedule_application_user_id",
            "schedule_application_result",
            ["user_id"],
        )
        op.create_index(
            "ix_schedule_application_root_revision",
            "schedule_application_result",
            ["root_plan_id", "created_revision"],
        )


def downgrade() -> None:
    op.drop_table("schedule_application_result")
    op.drop_table("schedule_trace")
    op.drop_table("schedule_draft")
    op.drop_table("schedule_candidate_slot")
    op.drop_table("schedule_availability_window")
    op.drop_table("schedule_candidate_set")
    op.drop_table("schedule_busy_interval")
    op.drop_table("schedule_busy_snapshot")
