"""Evolve Phase 2A orchestration tables into durable Phase 2B storage.

Revision ID: 0007_phase2b_orchestration_persistence
Revises: 0006_schedule_draft
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0007_phase2b_orchestration_persistence"
down_revision = "0006_schedule_draft"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return not context.is_offline_mode() and _inspector().has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(item["name"] == column for item in _inspector().get_columns(table))


def _has_index(table: str, name: str) -> bool:
    return any(item["name"] == name for item in _inspector().get_indexes(table))


def _has_unique(table: str, name: str) -> bool:
    return any(
        item.get("name") == name for item in _inspector().get_unique_constraints(table)
    )


def _add_column_if_missing(table: str, column: sa.Column[object]) -> None:
    if context.is_offline_mode() or not _has_column(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
    # Historical environments use a short Alembic version column.  The required
    # Phase 2B revision identifier is longer, so expand it before Alembic writes
    # the new head after this migration completes.
    if context.is_offline_mode() or (
        _has_table("alembic_version") and _has_column("alembic_version", "version_num")
    ):
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=128),
            existing_nullable=False,
        )
    if context.is_offline_mode() or (
        _has_table("checkpoint") and not _has_table("planning_checkpoint")
    ):
        op.rename_table("checkpoint", "planning_checkpoint")

    _add_column_if_missing(
        "planning_run",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )
    _add_column_if_missing(
        "planning_run",
        sa.Column("result_reference", sa.String(length=256), nullable=True),
    )
    op.execute(
        "UPDATE planning_run SET request_fingerprint = "
        "SHA2(CONCAT('legacy:', id), 256) WHERE request_fingerprint IS NULL"
    )
    op.alter_column(
        "planning_run",
        "request_fingerprint",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    if context.is_offline_mode() or not _has_unique(
        "planning_run", "uq_planning_run_request"
    ):
        op.create_unique_constraint(
            "uq_planning_run_request",
            "planning_run",
            ["user_id", "workflow_type", "client_request_id"],
        )

    _add_column_if_missing(
        "agent_step",
        sa.Column(
            "fencing_token",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    if context.is_offline_mode() or not _has_index("agent_step", "ix_agent_step_claim"):
        op.create_index(
            "ix_agent_step_claim",
            "agent_step",
            [
                "status",
                "next_execute_at",
                "priority",
                "sequence_no",
                "created_at",
                "id",
            ],
        )
    if context.is_offline_mode() or not _has_index("agent_step", "ix_agent_step_reap"):
        op.create_index(
            "ix_agent_step_reap",
            "agent_step",
            ["status", "lease_expires_at"],
        )

    _add_column_if_missing(
        "planning_checkpoint",
        sa.Column("step_type", sa.String(length=128), nullable=True),
    )
    _add_column_if_missing(
        "planning_checkpoint",
        sa.Column("run_status_after", sa.String(length=32), nullable=True),
    )
    _add_column_if_missing(
        "planning_checkpoint",
        sa.Column("step_status_after", sa.String(length=32), nullable=True),
    )
    op.execute(
        "UPDATE planning_checkpoint c JOIN agent_step s ON s.id = c.step_id "
        "SET c.step_type = s.step_type WHERE c.step_type IS NULL"
    )
    op.execute(
        "UPDATE planning_checkpoint SET run_status_after = 'COMPLETED' "
        "WHERE run_status_after IS NULL"
    )
    op.execute(
        "UPDATE planning_checkpoint SET step_status_after = 'SUCCEEDED' "
        "WHERE step_status_after IS NULL"
    )
    for column, length in (
        ("step_type", 128),
        ("run_status_after", 32),
        ("step_status_after", 32),
    ):
        op.alter_column(
            "planning_checkpoint",
            column,
            existing_type=sa.String(length=length),
            nullable=False,
        )

    for name, length in (
        ("from_status", 64),
        ("to_status", 64),
        ("worker_id", 128),
        ("error_code", 128),
    ):
        _add_column_if_missing(
            "audit_event", sa.Column(name, sa.String(length=length), nullable=True)
        )
    _add_column_if_missing("audit_event", sa.Column("attempt_no", sa.Integer()))


def downgrade() -> None:
    if context.is_offline_mode() or _has_table("audit_event"):
        for column in (
            "error_code",
            "attempt_no",
            "worker_id",
            "to_status",
            "from_status",
        ):
            if context.is_offline_mode() or _has_column("audit_event", column):
                op.drop_column("audit_event", column)

    if context.is_offline_mode() or _has_table("planning_checkpoint"):
        for column in ("step_status_after", "run_status_after", "step_type"):
            if context.is_offline_mode() or _has_column("planning_checkpoint", column):
                op.drop_column("planning_checkpoint", column)

    if context.is_offline_mode() or _has_table("agent_step"):
        for index in ("ix_agent_step_reap", "ix_agent_step_claim"):
            if context.is_offline_mode() or _has_index("agent_step", index):
                op.drop_index(index, table_name="agent_step")
        if context.is_offline_mode() or _has_column("agent_step", "fencing_token"):
            op.drop_column("agent_step", "fencing_token")

    if context.is_offline_mode() or _has_table("planning_run"):
        if context.is_offline_mode() or _has_unique(
            "planning_run", "uq_planning_run_request"
        ):
            op.drop_constraint(
                "uq_planning_run_request", "planning_run", type_="unique"
            )
        for column in ("result_reference", "request_fingerprint"):
            if context.is_offline_mode() or _has_column("planning_run", column):
                op.drop_column("planning_run", column)
    if context.is_offline_mode() or (
        _has_table("planning_checkpoint") and not _has_table("checkpoint")
    ):
        op.rename_table("planning_checkpoint", "checkpoint")
