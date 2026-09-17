"""MySQL persistence schema contracts independent of a live server."""

import importlib

from app.persistence.mysql.models import Base


def test_mysql_metadata_contains_primary_persistent_facts() -> None:
    required_tables = {
        "user_account",
        "fitness_profile",
        "user_constraint",
        "exercise_catalog",
        "weekly_plan",
        "workout_session",
        "session_exercise",
        "session_checkin",
        "memory_item",
        "memory_candidate",
        "memory_evidence",
        "context_snapshot",
        "planning_run",
        "agent_step",
        "planning_checkpoint",
        "audit_event",
        "idempotency_record",
        "worker_lease",
        "outbox",
        "ics_export",
    }

    assert required_tables <= set(Base.metadata.tables)


def test_user_account_email_and_checkin_event_are_unique_per_user() -> None:
    user_table = Base.metadata.tables["user_account"]
    checkin_table = Base.metadata.tables["session_checkin"]
    user_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in user_table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    checkin_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in checkin_table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert ("email",) in user_unique_columns
    assert ("user_id", "client_event_id") in checkin_unique_columns


def test_orchestration_metadata_has_phase_2b_fencing_and_checkpoint_contract() -> None:
    assert "planning_checkpoint" in Base.metadata.tables
    assert "checkpoint" not in Base.metadata.tables
    assert {"request_fingerprint", "result_reference"} <= set(
        Base.metadata.tables["planning_run"].columns.keys()
    )
    assert "fencing_token" in Base.metadata.tables["agent_step"].columns.keys()
    assert {
        "step_type",
        "run_status_after",
        "step_status_after",
    } <= set(Base.metadata.tables["planning_checkpoint"].columns.keys())
    assert Base.metadata.tables["planning_run"].c.status.type.length == 64
    assert (
        Base.metadata.tables["planning_checkpoint"].c.run_status_after.type.length == 64
    )


def test_phase_2b_migration_is_the_0006_successor() -> None:
    migration = importlib.import_module(
        "migrations.versions.0007_phase2b_orchestration_persistence"
    )

    assert migration.revision == "0007_phase2b_orchestration_persistence"
    assert migration.down_revision == "0006_schedule_draft"


def test_orchestration_status_capacity_migration_follows_phase_2b() -> None:
    migration = importlib.import_module(
        "migrations.versions.0008_orchestration_status_capacity"
    )

    assert migration.revision == "0008_orchestration_status_capacity"
    assert migration.down_revision == "0007_phase2b_orchestration_persistence"


def test_ics_export_migration_follows_orchestration_status_capacity() -> None:
    migration = importlib.import_module(
        "migrations.versions.0009_ics_export_persistence"
    )

    assert migration.revision == "0009_ics_export_persistence"
    assert migration.down_revision == "0008_orchestration_status_capacity"
