"""Complete the durable fields required by the existing Memory domain.

Revision ID: 0003_memory_ctx
Revises: 0002_mysql_persistence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_memory_ctx"
down_revision = "0002_mysql_persistence"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_if_missing(table_name: str, column: sa.Column[object]) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _index_if_missing(table_name: str, name: str, columns: list[str]) -> None:
    indexes = {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table_name)
    }
    if name not in indexes:
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    _add_if_missing(
        "memory_item", sa.Column("key", sa.String(length=80), nullable=True)
    )
    _add_if_missing(
        "memory_item", sa.Column("source", sa.String(length=64), nullable=True)
    )
    _add_if_missing(
        "memory_item", sa.Column("confidence", sa.Numeric(5, 4), nullable=True)
    )
    _add_if_missing(
        "memory_item",
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
    )
    _add_if_missing(
        "memory_item",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_if_missing(
        "memory_item",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    _index_if_missing(
        "memory_item", "ix_memory_item_user_status", ["user_id", "status"]
    )
    _index_if_missing(
        "memory_item",
        "ix_memory_item_user_type_status",
        ["user_id", "memory_type", "status"],
    )
    _add_if_missing(
        "memory_candidate",
        sa.Column("proposed_key", sa.String(length=80), nullable=True),
    )
    _add_if_missing(
        "memory_candidate",
        sa.Column("source_reference", sa.String(length=160), nullable=True),
    )
    _add_if_missing(
        "memory_candidate",
        sa.Column("evidence_summary", sa.String(length=240), nullable=True),
    )
    _add_if_missing(
        "memory_candidate", sa.Column("confidence", sa.Numeric(5, 4), nullable=True)
    )
    _add_if_missing(
        "memory_candidate",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_if_missing(
        "memory_candidate",
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
    )
    _add_if_missing(
        "memory_candidate",
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=True),
    )
    _index_if_missing(
        "memory_candidate", "ix_memory_candidate_user_status", ["user_id", "status"]
    )


def downgrade() -> None:
    for table_name, index_name in (
        ("memory_candidate", "ix_memory_candidate_user_status"),
        ("memory_item", "ix_memory_item_user_type_status"),
        ("memory_item", "ix_memory_item_user_status"),
    ):
        indexes = {
            item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table_name)
        }
        if index_name in indexes:
            op.drop_index(index_name, table_name=table_name)
    for table_name, column_name in (
        ("memory_candidate", "payload_fingerprint"),
        ("memory_candidate", "idempotency_key"),
        ("memory_candidate", "expires_at"),
        ("memory_candidate", "confidence"),
        ("memory_candidate", "evidence_summary"),
        ("memory_candidate", "source_reference"),
        ("memory_candidate", "proposed_key"),
        ("memory_item", "deleted_at"),
        ("memory_item", "confirmed_at"),
        ("memory_item", "valid_from"),
        ("memory_item", "confidence"),
        ("memory_item", "source"),
        ("memory_item", "key"),
    ):
        if column_name in _columns(table_name):
            op.drop_column(table_name, column_name)
