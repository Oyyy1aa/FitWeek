"""Persist Recovery Draft bundles and immutable Recovery artifacts.

Revision ID: 0011_recovery_draft_persistence
Revises: 0010_calendar_operation_persistence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0011_recovery_draft_persistence"
down_revision = "0010_calendar_operation_persistence"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("recovery_draft"):
        return

    time = mysql.DATETIME(fsp=6)
    op.create_table(
        "recovery_behavior_summary",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("window_start_utc", time, nullable=False),
        sa.Column("window_end_utc", time, nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("scheduled_session_count", sa.Integer(), nullable=False),
        sa.Column("checked_in_session_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("partially_completed_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("missing_checkin_count", sa.Integer(), nullable=False),
        sa.Column("completion_rate", sa.Numeric(8, 4)),
        sa.Column("participation_rate", sa.Numeric(8, 4)),
        sa.Column("rpe_sample_count", sa.Integer(), nullable=False),
        sa.Column("average_reported_rpe", sa.Numeric(8, 4)),
        sa.Column("high_reported_rpe_count", sa.Integer(), nullable=False),
        sa.Column("repeated_time_patterns", sa.JSON(), nullable=False),
        sa.Column("repeated_location_patterns", sa.JSON(), nullable=False),
        sa.Column("repeated_skip_patterns", sa.JSON(), nullable=False),
        sa.Column("evidence_references", sa.JSON(), nullable=False),
        sa.Column("conflict_checkin_ids", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
    )
    op.create_index(
        "ix_recovery_summary_user_created",
        "recovery_behavior_summary",
        ["user_id", "created_at"],
    )
    op.create_table(
        "recovery_change_impact",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("root_plan_id", sa.String(36), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("source_plan_version", sa.Integer(), nullable=False),
        sa.Column("mutable_session_ids", sa.JSON(), nullable=False),
        sa.Column("immutable_session_ids", sa.JSON(), nullable=False),
        sa.Column("preserved_session_ids", sa.JSON(), nullable=False),
        sa.Column("calendar_bound_session_ids", sa.JSON(), nullable=False),
        sa.Column("completed_checkin_ids", sa.JSON(), nullable=False),
        sa.Column("weekly_frequency_before", sa.Integer(), nullable=False),
        sa.Column("minimum_allowed_frequency", sa.Integer(), nullable=False),
        sa.Column("maximum_allowed_frequency", sa.Integer(), nullable=False),
        sa.Column("requires_session_redesign", sa.Boolean(), nullable=False),
        sa.Column("requires_schedule_redraft", sa.Boolean(), nullable=False),
        sa.Column("requires_calendar_reconciliation", sa.Boolean(), nullable=False),
        sa.Column("requires_new_plan_revision", sa.Boolean(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
    )
    op.create_index(
        "ix_recovery_impact_user_plan",
        "recovery_change_impact",
        ["user_id", "root_plan_id"],
    )
    op.create_table(
        "recovery_candidate_set",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("root_plan_id", sa.String(36), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("source_plan_version", sa.Integer(), nullable=False),
        sa.Column("behavior_summary_id", sa.String(36), nullable=False),
        sa.Column("behavior_summary_fingerprint", sa.String(64), nullable=False),
        sa.Column("context_snapshot_reference_id", sa.String(36), nullable=False),
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("change_impact_snapshot_id", sa.String(36), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(128), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.ForeignKeyConstraint(
            ["behavior_summary_id"], ["recovery_behavior_summary.id"]
        ),
        sa.ForeignKeyConstraint(
            ["change_impact_snapshot_id"], ["recovery_change_impact.id"]
        ),
    )
    op.create_index(
        "ix_recovery_candidate_user_plan",
        "recovery_candidate_set",
        ["user_id", "root_plan_id"],
    )
    op.create_table(
        "recovery_action_candidate",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("candidate_set_id", sa.String(36), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("target_session_id", sa.String(36)),
        sa.Column("target_week_start", sa.Date()),
        sa.Column("redesign_goal", sa.String(40)),
        sa.Column("evidence_pattern_ids", sa.JSON(), nullable=False),
        sa.Column("impact_snapshot_id", sa.String(36), nullable=False),
        sa.Column("requires_schedule_draft", sa.Boolean(), nullable=False),
        sa.Column("requires_session_design_draft", sa.Boolean(), nullable=False),
        sa.Column("requires_plan_revision", sa.Boolean(), nullable=False),
        sa.Column("requires_calendar_reconciliation", sa.Boolean(), nullable=False),
        sa.Column("deterministic_rank", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.ForeignKeyConstraint(["candidate_set_id"], ["recovery_candidate_set.id"]),
        sa.ForeignKeyConstraint(["impact_snapshot_id"], ["recovery_change_impact.id"]),
        sa.UniqueConstraint(
            "candidate_set_id",
            "sequence_no",
            name="uq_recovery_candidate_sequence",
        ),
    )
    op.create_index(
        "ix_recovery_candidate_draft_artifact",
        "recovery_action_candidate",
        ["candidate_set_id"],
    )
    op.create_table(
        "recovery_draft",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("client_request_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("request_payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("root_plan_id", sa.String(36), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("source_plan_version", sa.Integer(), nullable=False),
        sa.Column("behavior_summary_id", sa.String(36), nullable=False),
        sa.Column("context_snapshot_reference_id", sa.String(36), nullable=False),
        sa.Column("change_impact_snapshot_id", sa.String(36), nullable=False),
        sa.Column("candidate_set_id", sa.String(36), nullable=False),
        sa.Column("selected_action_candidate_ids", sa.JSON(), nullable=False),
        sa.Column("unresolved_session_ids", sa.JSON(), nullable=False),
        sa.Column("behavior_memory_proposal_ids", sa.JSON(), nullable=False),
        sa.Column("explanation_summary", sa.String(500), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("scope_status", sa.String(40), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.Column("expires_at", time, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reviewed_at", time),
        sa.Column("application_result_id", sa.String(36)),
        sa.Column("applied_root_plan_id", sa.String(36)),
        sa.Column("applied_source_revision", sa.Integer()),
        sa.Column("applied_created_revision", sa.Integer()),
        sa.Column("applied_session_ids", sa.JSON(), nullable=False),
        sa.Column("created_session_design_draft_ids", sa.JSON(), nullable=False),
        sa.Column("created_schedule_draft_ids", sa.JSON(), nullable=False),
        sa.Column("applied_at", time),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.ForeignKeyConstraint(
            ["behavior_summary_id"], ["recovery_behavior_summary.id"]
        ),
        sa.ForeignKeyConstraint(
            ["change_impact_snapshot_id"], ["recovery_change_impact.id"]
        ),
        sa.ForeignKeyConstraint(["candidate_set_id"], ["recovery_candidate_set.id"]),
        sa.UniqueConstraint(
            "user_id",
            "client_request_id",
            name="uq_recovery_draft_user_request",
        ),
    )
    op.create_index(
        "ix_recovery_draft_user_status",
        "recovery_draft",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_recovery_draft_user_plan",
        "recovery_draft",
        ["user_id", "root_plan_id"],
    )
    op.create_table(
        "recovery_memory_proposal",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("draft_id", sa.String(36), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("memory_type", sa.String(64), nullable=False),
        sa.Column("proposed_key", sa.String(128), nullable=False),
        sa.Column("proposed_value", sa.String(500), nullable=False),
        sa.Column("behavior_pattern_ids", sa.JSON(), nullable=False),
        sa.Column("evidence_checkin_ids", sa.JSON(), nullable=False),
        sa.Column("confidence_tier", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.Column("expires_at", time, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.ForeignKeyConstraint(["draft_id"], ["recovery_draft.id"]),
        sa.UniqueConstraint(
            "draft_id", "sequence_no", name="uq_recovery_proposal_sequence"
        ),
    )
    op.create_index(
        "ix_recovery_proposal_draft",
        "recovery_memory_proposal",
        ["draft_id"],
    )
    op.create_table(
        "recovery_trace",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("draft_id", sa.String(36), nullable=False, unique=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("behavior_summary_id", sa.String(36), nullable=False),
        sa.Column("behavior_summary_fingerprint", sa.String(64), nullable=False),
        sa.Column("context_snapshot_reference_id", sa.String(36), nullable=False),
        sa.Column("change_impact_snapshot_id", sa.String(36), nullable=False),
        sa.Column("candidate_set_id", sa.String(36), nullable=False),
        sa.Column("candidate_set_fingerprint", sa.String(64), nullable=False),
        sa.Column("scope_status", sa.String(40), nullable=False),
        sa.Column("provider_name", sa.String(120), nullable=False),
        sa.Column("provider_version", sa.String(120), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("validation_error_code", sa.String(128)),
        sa.Column("latency_ms", sa.Numeric(12, 3), nullable=False),
        sa.Column("created_at", time, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.ForeignKeyConstraint(["draft_id"], ["recovery_draft.id"]),
        sa.ForeignKeyConstraint(
            ["behavior_summary_id"], ["recovery_behavior_summary.id"]
        ),
        sa.ForeignKeyConstraint(
            ["change_impact_snapshot_id"], ["recovery_change_impact.id"]
        ),
        sa.ForeignKeyConstraint(["candidate_set_id"], ["recovery_candidate_set.id"]),
    )
    op.create_index(
        "ix_recovery_trace_user_created",
        "recovery_trace",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    if context.is_offline_mode() or _has_table("recovery_trace"):
        op.drop_table("recovery_trace")
        op.drop_table("recovery_memory_proposal")
        op.drop_table("recovery_draft")
        op.drop_table("recovery_action_candidate")
        op.drop_table("recovery_candidate_set")
        op.drop_table("recovery_change_impact")
        op.drop_table("recovery_behavior_summary")
