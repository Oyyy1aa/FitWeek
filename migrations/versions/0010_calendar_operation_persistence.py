"""Persist Calendar operation drafts, items, bindings, and attempts.

Revision ID: 0010_calendar_operation_persistence
Revises: 0009_ics_export_persistence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0010_calendar_operation_persistence"
down_revision = "0009_ics_export_persistence"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("calendar_operation_draft"):
        return

    time = mysql.DATETIME(fsp=6)
    op.create_table(
        "calendar_event_binding",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(120), nullable=False),
        sa.Column("calendar_id", sa.String(255), nullable=False),
        sa.Column("root_plan_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("stable_uid", sa.String(255), nullable=False),
        sa.Column("last_payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.Column("updated_at", time, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "calendar_id",
            "root_plan_id",
            "session_id",
            name="uq_calendar_binding_logical",
        ),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "calendar_id",
            "external_event_id",
            name="uq_calendar_binding_external",
        ),
    )
    op.create_table(
        "calendar_operation_draft",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("client_request_id", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(120), nullable=False),
        sa.Column("calendar_id", sa.String(255), nullable=False),
        sa.Column("root_plan_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.Column("updated_at", time, nullable=False),
        sa.Column("approved_at", time),
        sa.Column("rejected_at", time),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.UniqueConstraint(
            "user_id", "client_request_id", name="uq_calendar_draft_request"
        ),
    )
    op.create_index(
        "ix_calendar_draft_user_plan_revision",
        "calendar_operation_draft",
        ["user_id", "root_plan_id", "revision"],
    )
    op.create_index(
        "ix_calendar_draft_user_status",
        "calendar_operation_draft",
        ["user_id", "status"],
    )
    op.create_table(
        "calendar_operation_item",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("draft_id", sa.String(36), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("operation_type", sa.String(24), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("operation_key", sa.String(64), nullable=False),
        sa.Column("binding_id", sa.String(36)),
        sa.Column("stable_uid", sa.String(255)),
        sa.Column("summary", sa.String(512)),
        sa.Column("description", sa.Text()),
        sa.Column("start_at", time),
        sa.Column("end_at", time),
        sa.Column("timezone", sa.String(100)),
        sa.Column("transparency", sa.String(24)),
        sa.Column("payload_fingerprint", sa.String(64)),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(128)),
        sa.ForeignKeyConstraint(["draft_id"], ["calendar_operation_draft.id"]),
        sa.ForeignKeyConstraint(["binding_id"], ["calendar_event_binding.id"]),
        sa.UniqueConstraint(
            "draft_id", "sequence_no", name="uq_calendar_item_sequence"
        ),
        sa.UniqueConstraint("draft_id", "operation_key", name="uq_calendar_item_key"),
    )
    op.create_table(
        "calendar_operation_attempt",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("draft_id", sa.String(36), nullable=False),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("error_code", sa.String(128)),
        sa.Column("response_reference_hash", sa.String(64)),
        sa.Column("started_at", time, nullable=False),
        sa.Column("finished_at", time, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.ForeignKeyConstraint(["draft_id"], ["calendar_operation_draft.id"]),
        sa.ForeignKeyConstraint(["item_id"], ["calendar_operation_item.id"]),
        sa.UniqueConstraint("item_id", "attempt_no", name="uq_calendar_attempt_number"),
    )


def downgrade() -> None:
    if context.is_offline_mode() or _has_table("calendar_operation_attempt"):
        op.drop_table("calendar_operation_attempt")
        op.drop_table("calendar_operation_item")
        op.drop_index(
            "ix_calendar_draft_user_status", table_name="calendar_operation_draft"
        )
        op.drop_index(
            "ix_calendar_draft_user_plan_revision",
            table_name="calendar_operation_draft",
        )
        op.drop_table("calendar_operation_draft")
        op.drop_table("calendar_event_binding")
