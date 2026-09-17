"""Normalized SQLAlchemy mappings for FitWeek's MySQL source of truth."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME, LONGBLOB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base metadata owned by the FitWeek Alembic migrations."""


class UserAccountModel(Base):
    __tablename__ = "user_account"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class FitnessProfileModel(Base):
    __tablename__ = "fitness_profile"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, unique=True, index=True
    )
    experience_level: Mapped[str] = mapped_column(String(32), nullable=False)
    primary_goal: Mapped[str] = mapped_column(String(64), nullable=False)
    weekly_frequency: Mapped[int] = mapped_column(Integer, nullable=False)
    max_session_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class UserConstraintModel(Base):
    __tablename__ = "user_constraint"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("fitness_profile.id"), nullable=False, index=True
    )
    constraint_type: Mapped[str] = mapped_column(String(64), nullable=False)
    constraint_value: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    is_hard: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class ExerciseCatalogModel(Base):
    __tablename__ = "exercise_catalog"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    equipment: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class WeeklyPlanModel(Base):
    __tablename__ = "weekly_plan"
    __table_args__ = (
        UniqueConstraint("user_id", "series_id", "revision", name="uq_plan_revision"),
        Index("ix_weekly_plan_user_week", "user_id", "week_start"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    series_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    constraint_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    estimated_total_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    generation_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    change_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    parent_revision: Mapped[int | None] = mapped_column(Integer)
    revision_reason: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class WorkoutSessionModel(Base):
    __tablename__ = "workout_session"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "logical_session_id",
            name="uq_workout_session_plan_logical",
        ),
        Index("ix_workout_session_logical", "logical_session_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    logical_session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("weekly_plan.id"), nullable=False, index=True
    )
    scheduled_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    scheduled_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    location_type: Mapped[str] = mapped_column(String(32), nullable=False)
    session_type: Mapped[str] = mapped_column(String(32), nullable=False)
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    target_difficulty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    schedule_source_metadata: Mapped[list[list[str]]] = mapped_column(
        JSON, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class SessionExerciseModel(Base):
    __tablename__ = "session_exercise"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence_no", name="uq_session_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("workout_session.id"), nullable=False, index=True
    )
    exercise_id: Mapped[str] = mapped_column(
        ForeignKey("exercise_catalog.id"), nullable=False, index=True
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sets: Mapped[int | None] = mapped_column(Integer)
    repetitions: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    rest_seconds: Mapped[int] = mapped_column(Integer, nullable=False)


class SessionCheckinModel(Base):
    __tablename__ = "session_checkin"
    __table_args__ = (
        UniqueConstraint("user_id", "client_event_id", name="uq_checkin_user_event"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(ForeignKey("weekly_plan.id"), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("workout_session.id"), nullable=False, index=True
    )
    client_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    actual_minutes: Mapped[int | None] = mapped_column(Integer)
    perceived_effort: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class MemoryItemModel(Base):
    __tablename__ = "memory_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_content: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    memory_key: Mapped[str | None] = mapped_column("key", String(80), index=True)
    source: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class MemoryCandidateModel(Base):
    __tablename__ = "memory_candidate"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    proposed_content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    proposed_key: Mapped[str | None] = mapped_column(String(80), index=True)
    source_reference: Mapped[str | None] = mapped_column(String(160))
    evidence_summary: Mapped[str | None] = mapped_column(String(240))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    payload_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class MemoryEvidenceModel(Base):
    __tablename__ = "memory_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(
        ForeignKey("memory_item.id"), nullable=False, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reference: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ContextSnapshotModel(Base):
    __tablename__ = "context_snapshot"
    __table_args__ = (
        UniqueConstraint("user_id", "agent_type", "scope_id", name="uq_context_scope"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    character_count: Mapped[int] = mapped_column(Integer, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ProfileDraftModel(Base):
    """Durable, user-reviewed Profile Agent output; never the formal Profile."""

    __tablename__ = "profile_draft"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "client_request_id", name="uq_profile_draft_request"
        ),
        UniqueConstraint("user_id", "apply_request_id", name="uq_profile_draft_apply"),
        UniqueConstraint(
            "user_id", "reject_request_id", name="uq_profile_draft_reject"
        ),
        Index("ix_profile_draft_user_status", "user_id", "status"),
        Index("ix_profile_draft_expires_at", "expires_at"),
        Index("ix_profile_draft_context_snapshot", "context_snapshot_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_summary: Mapped[str] = mapped_column(String(240), nullable=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fallback_type: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    context_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("context_snapshot.id")
    )
    context_fingerprint: Mapped[str | None] = mapped_column(String(64))
    context_contract_version: Mapped[str | None] = mapped_column(String(64))
    context_policy_version: Mapped[str | None] = mapped_column(String(64))
    context_degraded_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    context_included_memory_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    rejected_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    applied_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("fitness_profile.id")
    )
    apply_request_id: Mapped[str | None] = mapped_column(String(128))
    apply_fingerprint: Mapped[str | None] = mapped_column(String(64))
    apply_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reject_request_id: Mapped[str | None] = mapped_column(String(128))
    reject_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class SessionDesignCandidateSetModel(Base):
    """Immutable, deterministic candidate set used by one Session Design."""

    __tablename__ = "session_design_candidate_set"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "request_fingerprint",
            "fingerprint",
            name="uq_session_design_candidate_fingerprint",
        ),
        Index(
            "ix_session_design_candidate_context",
            "context_snapshot_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(64), nullable=False)
    context_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("context_snapshot.id"), nullable=False
    )
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class SessionDesignCandidateSlotModel(Base):
    """One ordered template slot in a frozen candidate set."""

    __tablename__ = "session_design_candidate_slot"
    __table_args__ = (
        UniqueConstraint(
            "candidate_set_id",
            "slot_id",
            name="uq_session_design_candidate_slot",
        ),
        UniqueConstraint(
            "candidate_set_id",
            "candidate_order",
            name="uq_session_design_candidate_order",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_set_id: Mapped[str] = mapped_column(
        ForeignKey("session_design_candidate_set.id"),
        nullable=False,
        index=True,
    )
    slot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    exercise_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    candidate_order: Mapped[int] = mapped_column(Integer, nullable=False)


class SessionDesignDraftModel(Base):
    """Durable review state for a controlled Session Design proposal."""

    __tablename__ = "session_design_draft"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "client_request_id",
            name="uq_session_design_draft_request",
        ),
        Index(
            "ix_session_design_draft_user_status",
            "user_id",
            "status",
        ),
        Index("ix_session_design_draft_expires_at", "expires_at"),
        Index(
            "ix_session_design_draft_candidate_set",
            "candidate_set_id",
        ),
        Index(
            "ix_session_design_draft_context",
            "context_snapshot_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    request_payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_set_id: Mapped[str] = mapped_column(
        ForeignKey("session_design_candidate_set.id"), nullable=False
    )
    candidate_set_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    context_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("context_snapshot.id"), nullable=False
    )
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    context_degraded_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(64), nullable=False)
    session_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    target_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    location: Mapped[str] = mapped_column(String(32), nullable=False)
    goal: Mapped[str] = mapped_column(String(64), nullable=False)
    exercises: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    exercise_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    duration: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    safety_validation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_summary: Mapped[str] = mapped_column(String(240), nullable=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False)
    explanation_summary: Mapped[str] = mapped_column(String(600), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    applied_root_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("weekly_plan.id")
    )
    applied_revision: Mapped[int | None] = mapped_column(Integer)
    applied_session_id: Mapped[str | None] = mapped_column(String(36))
    application_result_id: Mapped[str | None] = mapped_column(String(36))
    applied_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class SessionDesignTraceModel(Base):
    """Safe trace references for a Session Design Draft."""

    __tablename__ = "session_design_trace"

    draft_id: Mapped[str] = mapped_column(
        ForeignKey("session_design_draft.id"), primary_key=True
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    candidate_set_id: Mapped[str] = mapped_column(
        ForeignKey("session_design_candidate_set.id"), nullable=False
    )
    candidate_set_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    context_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("context_snapshot.id"), nullable=False
    )
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_summary: Mapped[str] = mapped_column(String(240), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False)
    validation_error_code: Mapped[str | None] = mapped_column(String(128))
    model_trace_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)


class SessionDesignApplicationResultModel(Base):
    """Idempotent record of atomically applying one accepted Draft."""

    __tablename__ = "session_design_application_result"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "client_request_id",
            name="uq_session_design_application_request",
        ),
        UniqueConstraint(
            "draft_id",
            name="uq_session_design_application_draft",
        ),
        Index(
            "ix_session_design_application_root_revision",
            "root_plan_id",
            "created_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    application_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("session_design_draft.id"), nullable=False
    )
    root_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    previous_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class ScheduleBusySnapshotModel(Base):
    """Privacy-minimal immutable Busy Snapshot for one scheduling request."""

    __tablename__ = "schedule_busy_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "fingerprint",
            name="uq_schedule_busy_snapshot_fingerprint",
        ),
        Index(
            "ix_schedule_busy_snapshot_user_created",
            "user_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    range_start_utc: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    range_end_utc: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_summary: Mapped[str] = mapped_column(String(240), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class ScheduleBusyIntervalModel(Base):
    """Opaque UTC busy interval; no Calendar title/body/attendee data."""

    __tablename__ = "schedule_busy_interval"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "interval_order",
            name="uq_schedule_busy_interval_order",
        ),
        Index(
            "ix_schedule_busy_interval_user_start",
            "user_id",
            "starts_at_utc",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_busy_snapshot.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    starts_at_utc: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    ends_at_utc: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    interval_order: Mapped[int] = mapped_column(Integer, nullable=False)


class ScheduleCandidateSetModel(Base):
    """Frozen deterministic Time Slot Candidate Set."""

    __tablename__ = "schedule_candidate_set"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "fingerprint",
            name="uq_schedule_candidate_fingerprint",
        ),
        Index(
            "ix_schedule_candidate_user_revision",
            "user_id",
            "root_plan_id",
            "source_revision",
        ),
        Index("ix_schedule_candidate_context", "context_snapshot_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    root_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    busy_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_busy_snapshot.id"), nullable=False
    )
    context_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("context_snapshot.id"), nullable=False
    )
    session_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class ScheduleAvailabilityWindowModel(Base):
    """Normalized immutable Availability input for one Candidate Set."""

    __tablename__ = "schedule_availability_window"
    __table_args__ = (
        UniqueConstraint(
            "candidate_set_id",
            "window_order",
            name="uq_schedule_availability_order",
        ),
        Index(
            "ix_schedule_availability_user_start",
            "user_id",
            "starts_at_utc",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_set_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_candidate_set.id"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    starts_at_utc: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    ends_at_utc: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    location: Mapped[str] = mapped_column(String(32), nullable=False)
    window_order: Mapped[int] = mapped_column(Integer, nullable=False)


class ScheduleCandidateSlotModel(Base):
    """One immutable Agent-selectable UTC slot."""

    __tablename__ = "schedule_candidate_slot"
    __table_args__ = (
        UniqueConstraint(
            "candidate_set_id",
            "slot_id",
            name="uq_schedule_candidate_slot",
        ),
        UniqueConstraint(
            "candidate_set_id",
            "candidate_order",
            name="uq_schedule_candidate_order",
        ),
        Index(
            "ix_schedule_candidate_slot_session_start",
            "session_id",
            "starts_at_utc",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_set_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_candidate_set.id"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    slot_id: Mapped[str] = mapped_column(String(120), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    starts_at_utc: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    ends_at_utc: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    location: Mapped[str] = mapped_column(String(32), nullable=False)
    preference_score: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    source_availability_id: Mapped[str] = mapped_column(String(36), nullable=False)
    candidate_order: Mapped[int] = mapped_column(Integer, nullable=False)


class ScheduleDraftModel(Base):
    """Durable review state for one frozen Schedule proposal."""

    __tablename__ = "schedule_draft"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "client_request_id",
            name="uq_schedule_draft_request",
        ),
        Index("ix_schedule_draft_user_status", "user_id", "status"),
        Index("ix_schedule_draft_expires_at", "expires_at"),
        Index("ix_schedule_draft_candidate", "candidate_set_id"),
        Index("ix_schedule_draft_context", "context_snapshot_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    request_payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    root_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    busy_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_busy_snapshot.id"), nullable=False
    )
    candidate_set_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_candidate_set.id"), nullable=False
    )
    candidate_set_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    context_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("context_snapshot.id"), nullable=False
    )
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    context_degraded_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    assignments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    unresolved: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_summary: Mapped[str] = mapped_column(String(240), nullable=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False)
    calendar_verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    explanation_summary: Mapped[str] = mapped_column(String(600), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    applied_root_plan_id: Mapped[str | None] = mapped_column(String(36))
    applied_source_revision: Mapped[int | None] = mapped_column(Integer)
    applied_created_revision: Mapped[int | None] = mapped_column(Integer)
    application_result_id: Mapped[str | None] = mapped_column(String(36))
    applied_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class ScheduleTraceModel(Base):
    """Safe Schedule trace metadata; no Calendar payload or prompt."""

    __tablename__ = "schedule_trace"

    draft_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_draft.id"), primary_key=True
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    busy_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_busy_snapshot.id"), nullable=False
    )
    candidate_set_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_candidate_set.id"), nullable=False
    )
    candidate_set_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    context_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("context_snapshot.id"), nullable=False
    )
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_summary: Mapped[str] = mapped_column(String(240), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_version: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_error_code: Mapped[str | None] = mapped_column(String(128))
    latency_ms: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False)
    provider_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    calendar_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    calendar_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    model_trace_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class ScheduleApplicationResultModel(Base):
    """Idempotent record of one atomic Schedule Draft application."""

    __tablename__ = "schedule_application_result"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "client_request_id",
            name="uq_schedule_application_request",
        ),
        UniqueConstraint("draft_id", name="uq_schedule_application_draft"),
        Index(
            "ix_schedule_application_root_revision",
            "root_plan_id",
            "created_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    application_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_draft.id"), nullable=False
    )
    root_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    calendar_verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class IcsExportModel(Base):
    """Immutable bytes and metadata for one durable ICS export."""

    __tablename__ = "ics_export"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "client_request_id",
            name="uq_ics_export_user_request",
        ),
        Index(
            "ix_ics_export_user_plan_revision",
            "user_id",
            "root_plan_id",
            "revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    root_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[bytes] = mapped_column(LONGBLOB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class CalendarEventBindingModel(Base):
    __tablename__ = "calendar_event_binding"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "calendar_id",
            "root_plan_id",
            "session_id",
            name="uq_calendar_binding_logical",
        ),
        UniqueConstraint(
            "user_id",
            "provider",
            "calendar_id",
            "external_event_id",
            name="uq_calendar_binding_external",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(255), nullable=False)
    root_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stable_uid: Mapped[str] = mapped_column(String(255), nullable=False)
    last_payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class CalendarOperationDraftModel(Base):
    __tablename__ = "calendar_operation_draft"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "client_request_id", name="uq_calendar_draft_request"
        ),
        Index(
            "ix_calendar_draft_user_plan_revision",
            "user_id",
            "root_plan_id",
            "revision",
        ),
        Index("ix_calendar_draft_user_status", "user_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(255), nullable=False)
    root_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    rejected_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class CalendarOperationItemModel(Base):
    __tablename__ = "calendar_operation_item"
    __table_args__ = (
        UniqueConstraint("draft_id", "sequence_no", name="uq_calendar_item_sequence"),
        UniqueConstraint("draft_id", "operation_key", name="uq_calendar_item_key"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("calendar_operation_draft.id"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("calendar_event_binding.id")
    )
    stable_uid: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text)
    start_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    end_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    timezone: Mapped[str | None] = mapped_column(String(100))
    transparency: Mapped[str | None] = mapped_column(String(24))
    payload_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(128))


class CalendarOperationAttemptModel(Base):
    __tablename__ = "calendar_operation_attempt"
    __table_args__ = (
        UniqueConstraint("item_id", "attempt_no", name="uq_calendar_attempt_number"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("calendar_operation_draft.id"), nullable=False
    )
    item_id: Mapped[str] = mapped_column(
        ForeignKey("calendar_operation_item.id"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    response_reference_hash: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class RecoveryBehaviorSummaryModel(Base):
    __tablename__ = "recovery_behavior_summary"
    __table_args__ = (
        Index("ix_recovery_summary_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    window_start_utc: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    window_end_utc: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    scheduled_session_count: Mapped[int] = mapped_column(Integer, nullable=False)
    checked_in_session_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    partially_completed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_checkin_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    participation_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    rpe_sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    average_reported_rpe: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    high_reported_rpe_count: Mapped[int] = mapped_column(Integer, nullable=False)
    repeated_time_patterns: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    repeated_location_patterns: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    repeated_skip_patterns: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    evidence_references: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    conflict_checkin_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class RecoveryChangeImpactModel(Base):
    __tablename__ = "recovery_change_impact"
    __table_args__ = (Index("ix_recovery_impact_user_plan", "user_id", "root_plan_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    root_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    mutable_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    immutable_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    preserved_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    calendar_bound_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    completed_checkin_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    weekly_frequency_before: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_allowed_frequency: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_allowed_frequency: Mapped[int] = mapped_column(Integer, nullable=False)
    requires_session_redesign: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_schedule_redraft: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_calendar_reconciliation: Mapped[bool] = mapped_column(
        Boolean, nullable=False
    )
    requires_new_plan_revision: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class RecoveryCandidateSetModel(Base):
    __tablename__ = "recovery_candidate_set"
    __table_args__ = (
        Index("ix_recovery_candidate_user_plan", "user_id", "root_plan_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    root_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    behavior_summary_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_behavior_summary.id"), nullable=False
    )
    behavior_summary_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    context_snapshot_reference_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    change_impact_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_change_impact.id"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class RecoveryActionCandidateModel(Base):
    __tablename__ = "recovery_action_candidate"
    __table_args__ = (
        UniqueConstraint(
            "candidate_set_id",
            "sequence_no",
            name="uq_recovery_candidate_sequence",
        ),
        Index("ix_recovery_candidate_draft_artifact", "candidate_set_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    candidate_set_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_candidate_set.id"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_session_id: Mapped[str | None] = mapped_column(String(36))
    target_week_start: Mapped[date | None] = mapped_column(Date)
    redesign_goal: Mapped[str | None] = mapped_column(String(40))
    evidence_pattern_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    impact_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_change_impact.id"), nullable=False
    )
    requires_schedule_draft: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_session_design_draft: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_plan_revision: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_calendar_reconciliation: Mapped[bool] = mapped_column(
        Boolean, nullable=False
    )
    deterministic_rank: Mapped[int] = mapped_column(Integer, nullable=False)


class RecoveryDraftModel(Base):
    __tablename__ = "recovery_draft"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "client_request_id",
            name="uq_recovery_draft_user_request",
        ),
        Index("ix_recovery_draft_user_status", "user_id", "status"),
        Index("ix_recovery_draft_user_plan", "user_id", "root_plan_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    request_payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    root_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    behavior_summary_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_behavior_summary.id"), nullable=False
    )
    context_snapshot_reference_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    change_impact_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_change_impact.id"), nullable=False
    )
    candidate_set_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_candidate_set.id"), nullable=False
    )
    selected_action_candidate_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False
    )
    unresolved_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    behavior_memory_proposal_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False
    )
    explanation_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scope_status: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    application_result_id: Mapped[str | None] = mapped_column(String(36))
    applied_root_plan_id: Mapped[str | None] = mapped_column(String(36))
    applied_source_revision: Mapped[int | None] = mapped_column(Integer)
    applied_created_revision: Mapped[int | None] = mapped_column(Integer)
    applied_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_session_design_draft_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False
    )
    created_schedule_draft_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))


class RecoveryApplicationResultModel(Base):
    """Durable, idempotent fact for one Recovery Draft application."""

    __tablename__ = "recovery_application_result"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "client_request_id", name="uq_recovery_application_request"
        ),
        UniqueConstraint("recovery_draft_id", name="uq_recovery_application_draft"),
        Index("ix_recovery_application_user_result", "user_id", "id"),
        Index("ix_recovery_application_user_draft", "user_id", "recovery_draft_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    application_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    recovery_draft_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_draft.id"), nullable=False
    )
    root_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_revision: Mapped[int | None] = mapped_column(Integer)
    applied_action_candidate_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False
    )
    session_design_draft_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    schedule_draft_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    affected_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    removed_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    preserved_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    immutable_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class RecoverySessionDesignSubdraftBindingModel(Base):
    __tablename__ = "recovery_session_design_subdraft_binding"
    __table_args__ = (
        UniqueConstraint(
            "recovery_draft_id",
            "candidate_id",
            name="uq_recovery_session_design_binding",
        ),
        Index(
            "ix_recovery_session_design_binding_lookup",
            "recovery_draft_id",
            "candidate_id",
        ),
    )

    recovery_draft_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_draft.id"), primary_key=True
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_action_candidate.id"), primary_key=True
    )
    child_draft_id: Mapped[str] = mapped_column(
        ForeignKey("session_design_draft.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class RecoveryScheduleSubdraftBindingModel(Base):
    __tablename__ = "recovery_schedule_subdraft_binding"
    __table_args__ = (
        UniqueConstraint(
            "recovery_draft_id", "candidate_id", name="uq_recovery_schedule_binding"
        ),
        Index(
            "ix_recovery_schedule_binding_lookup", "recovery_draft_id", "candidate_id"
        ),
    )

    recovery_draft_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_draft.id"), primary_key=True
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_action_candidate.id"), primary_key=True
    )
    child_draft_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_draft.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class RecoveryMemoryProposalImportModel(Base):
    __tablename__ = "recovery_memory_proposal_import"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "client_request_id", name="uq_recovery_memory_import_request"
        ),
        Index("ix_recovery_memory_import_user_draft", "user_id", "draft_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_draft.id"), nullable=False
    )
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    memory_candidate_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class RecoveryMemoryProposalModel(Base):
    __tablename__ = "recovery_memory_proposal"
    __table_args__ = (
        UniqueConstraint(
            "draft_id", "sequence_no", name="uq_recovery_proposal_sequence"
        ),
        Index("ix_recovery_proposal_draft", "draft_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_draft.id"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_key: Mapped[str] = mapped_column(String(128), nullable=False)
    proposed_value: Mapped[str] = mapped_column(String(500), nullable=False)
    behavior_pattern_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence_checkin_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    confidence_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class RecoveryTraceModel(Base):
    __tablename__ = "recovery_trace"
    __table_args__ = (Index("ix_recovery_trace_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id"), nullable=False)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_draft.id"), nullable=False, unique=True
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    behavior_summary_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_behavior_summary.id"), nullable=False
    )
    behavior_summary_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    context_snapshot_reference_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    change_impact_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_change_impact.id"), nullable=False
    )
    candidate_set_id: Mapped[str] = mapped_column(
        ForeignKey("recovery_candidate_set.id"), nullable=False
    )
    candidate_set_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_status: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_version: Mapped[str] = mapped_column(String(120), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False)
    validation_error_code: Mapped[str | None] = mapped_column(String(128))
    latency_ms: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class PlanningRunModel(Base):
    __tablename__ = "planning_run"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "workflow_type",
            "client_request_id",
            name="uq_planning_run_request",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    workflow_type: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result_reference: Mapped[str | None] = mapped_column(String(256))
    current_step_id: Mapped[str | None] = mapped_column(String(36))
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentStepModel(Base):
    __tablename__ = "agent_step"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no", name="uq_run_step_sequence"),
        Index(
            "ix_agent_step_claim",
            "status",
            "next_execute_at",
            "priority",
            "sequence_no",
            "created_at",
            "id",
        ),
        Index("ix_agent_step_reap", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("planning_run.id"), nullable=False, index=True
    )
    step_type: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_execute_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), unique=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class StepDependencyModel(Base):
    __tablename__ = "step_dependency"

    step_id: Mapped[str] = mapped_column(ForeignKey("agent_step.id"), primary_key=True)
    dependency_step_id: Mapped[str] = mapped_column(
        ForeignKey("agent_step.id"), primary_key=True
    )


class CheckpointModel(Base):
    __tablename__ = "planning_checkpoint"
    __table_args__ = (
        UniqueConstraint("step_id", "attempt_no", name="uq_checkpoint_attempt"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("planning_run.id"), nullable=False, index=True
    )
    step_id: Mapped[str] = mapped_column(
        ForeignKey("agent_step.id"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(128), nullable=False)
    handler_version: Mapped[str] = mapped_column(String(128), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    output_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_reference: Mapped[str | None] = mapped_column(String(256))
    run_status_after: Mapped[str] = mapped_column(String(64), nullable=False)
    step_status_after: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class AuditEventModel(Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no", name="uq_audit_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("planning_run.id"), index=True
    )
    step_id: Mapped[str | None] = mapped_column(ForeignKey("agent_step.id"), index=True)
    sequence_no: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(64))
    to_status: Mapped[str | None] = mapped_column(String(64))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    attempt_no: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(128))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class IdempotencyRecordModel(Base):
    __tablename__ = "idempotency_record"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "operation", "request_key", name="uq_idempotency_request"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class WorkerLeaseModel(Base):
    __tablename__ = "worker_lease"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class OutboxModel(Base):
    __tablename__ = "outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.id"), nullable=False, index=True
    )
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
