"""MySQL persistence for Profile Agent drafts and atomic review application."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.common import RepositoryUniqueError, utc_now
from app.domain.model_gateway.enums import FallbackType
from app.domain.profile_agent.apply_models import (
    ProfileDraftApplyResult,
    ProfileDraftCommitResult,
    ProfileDraftMergeOutcome,
    ProfileDraftRejectResult,
)
from app.domain.profile_agent.errors import (
    ProfileDraftAlreadyAppliedError,
    ProfileDraftApplyIdempotencyConflictError,
    ProfileDraftExpiredError,
    ProfileDraftNotFoundError,
    ProfileDraftRejectedError,
    ProfileDraftRejectIdempotencyConflictError,
    ProfileDraftVersionConflictError,
    ProfileStateConflictError,
    ProfileVersionConflictError,
)
from app.domain.profile_agent.models import (
    ProfileAgentDraft,
    ProfileAgentOutput,
    ProfileDraftStatus,
)
from app.persistence.mysql.models import (
    AuditEventModel,
    FitnessProfileModel,
    ProfileDraftModel,
    UserConstraintModel,
)


class MySQLProfileDraftRepository:
    """One adapter for the existing draft and review repository protocols."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    async def get(self, draft_id: UUID, user_id: UUID) -> ProfileAgentDraft | None:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(ProfileDraftModel)
                    .where(
                        ProfileDraftModel.id == str(draft_id),
                        ProfileDraftModel.user_id == str(user_id),
                    )
                    .with_for_update()
                )
                return self._active_or_none(session, row)

    async def get_by_client_request_id(
        self, user_id: UUID, client_request_id: str
    ) -> ProfileAgentDraft | None:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(ProfileDraftModel)
                    .where(
                        ProfileDraftModel.user_id == str(user_id),
                        ProfileDraftModel.client_request_id == client_request_id,
                    )
                    .with_for_update()
                )
                return self._active_or_none(session, row)

    async def list(
        self,
        user_id: UUID,
        *,
        status: ProfileDraftStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[ProfileAgentDraft]:
        async with self._sessions() as session:
            async with session.begin():
                statement = select(ProfileDraftModel).where(
                    ProfileDraftModel.user_id == str(user_id)
                )
                if status is not None:
                    statement = statement.where(
                        ProfileDraftModel.status == status.value
                    )
                rows = (
                    await session.scalars(
                        statement.order_by(
                            ProfileDraftModel.created_at.desc(), ProfileDraftModel.id
                        )
                        .offset(offset)
                        .limit(limit)
                        .with_for_update()
                    )
                ).all()
                now = self._clock()
                for row in rows:
                    if (
                        row.status == ProfileDraftStatus.PENDING_REVIEW.value
                        and self._utc(row.expires_at) <= now
                    ):
                        row.status = ProfileDraftStatus.EXPIRED.value
                        row.updated_at = self._db_time(now)
                        row.version += 1
                        self._audit(
                            session,
                            user_id,
                            "PROFILE_DRAFT_EXPIRED",
                            {"draft_id": row.id},
                            now,
                        )
                return [self._draft_from_row(row) for row in rows]

    async def save(self, draft: ProfileAgentDraft) -> ProfileAgentDraft:
        async with self._sessions() as session:
            try:
                async with session.begin():
                    if await session.get(ProfileDraftModel, str(draft.id)) is not None:
                        raise RepositoryUniqueError("profile_draft.id", draft.id)
                    session.add(self._row_from_draft(draft))
                    self._audit(
                        session,
                        draft.user_id,
                        "PROFILE_DRAFT_CREATED",
                        {"draft_id": str(draft.id)},
                        draft.created_at,
                    )
            except IntegrityError as exc:
                raise RepositoryUniqueError(
                    "profile_draft.user_id_client_request_id", draft.user_id
                ) from exc
        return draft

    async def reset(self) -> None:
        """Test-only reset; refuse any database not explicitly named fitweek_test."""

        async with self._sessions() as session:
            async with session.begin():
                database_name = await session.scalar(text("DATABASE()"))
                if database_name != "fitweek_test":
                    raise RuntimeError(
                        "Profile draft reset is only permitted for fitweek_test"
                    )
                await session.execute(delete(ProfileDraftModel))

    async def get_draft_for_review(
        self, draft_id: UUID, user_id: UUID
    ) -> ProfileAgentDraft | None:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(ProfileDraftModel)
                    .where(
                        ProfileDraftModel.id == str(draft_id),
                        ProfileDraftModel.user_id == str(user_id),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                now = self._clock()
                if (
                    row.status == ProfileDraftStatus.PENDING_REVIEW.value
                    and self._utc(row.expires_at) <= now
                ):
                    row.status = ProfileDraftStatus.EXPIRED.value
                    row.updated_at = self._db_time(now)
                    row.version += 1
                    self._audit(
                        session,
                        user_id,
                        "PROFILE_DRAFT_EXPIRED",
                        {"draft_id": row.id},
                        now,
                    )
                return self._draft_from_row(row)

    async def get_apply_result(
        self, draft_id: UUID, user_id: UUID
    ) -> ProfileDraftApplyResult | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ProfileDraftModel).where(
                    ProfileDraftModel.id == str(draft_id),
                    ProfileDraftModel.user_id == str(user_id),
                )
            )
            if row is None or row.apply_result is None:
                return None
            return self._apply_result_from_payload(row.apply_result)

    async def commit_apply(
        self,
        *,
        draft_id: UUID,
        user_id: UUID,
        expected_draft_version: int,
        expected_profile_version: int | None,
        merge: ProfileDraftMergeOutcome,
        result: ProfileDraftApplyResult,
        now: datetime,
    ) -> ProfileDraftCommitResult:
        async with self._sessions() as session:
            try:
                async with session.begin():
                    existing = await session.scalar(
                        select(ProfileDraftModel)
                        .where(
                            ProfileDraftModel.user_id == str(user_id),
                            ProfileDraftModel.apply_request_id
                            == result.client_request_id,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        if (
                            existing.id != str(draft_id)
                            or existing.apply_fingerprint != result.apply_fingerprint
                            or existing.apply_result is None
                        ):
                            raise ProfileDraftApplyIdempotencyConflictError(
                                "apply request id belongs to another payload"
                            )
                        return ProfileDraftCommitResult(
                            result=self._apply_result_from_payload(
                                existing.apply_result
                            ),
                            created=False,
                        )
                    draft = await self._locked_draft(session, draft_id, user_id)
                    self._validate_pending(draft, expected_draft_version, now)
                    profile_row = await session.scalar(
                        select(FitnessProfileModel)
                        .where(FitnessProfileModel.user_id == str(user_id))
                        .with_for_update()
                    )
                    constraint_rows = (
                        await session.scalars(
                            select(UserConstraintModel)
                            .where(
                                UserConstraintModel.profile_id == str(merge.profile.id)
                            )
                            .with_for_update()
                        )
                    ).all()
                    current_constraint_versions = tuple(
                        sorted(
                            (UUID(item.id), item.version) for item in constraint_rows
                        )
                    )
                    if (
                        current_constraint_versions
                        != merge.expected_constraint_versions
                    ):
                        raise ProfileStateConflictError("profile constraints changed")
                    if any(
                        any(row.id == str(item.id) for row in constraint_rows)
                        for item in merge.constraints_to_add
                    ):
                        raise ProfileStateConflictError("constraint candidates changed")
                    self._apply_profile(profile_row, merge, expected_profile_version)
                    if profile_row is None:
                        profile_row = self._profile_row(merge)
                        session.add(profile_row)
                    for constraint in merge.constraints_to_add:
                        session.add(
                            UserConstraintModel(
                                id=str(constraint.id),
                                profile_id=str(constraint.profile_id),
                                constraint_type=constraint.constraint_type.value,
                                constraint_value=constraint.constraint_value,
                                priority=constraint.priority,
                                is_hard=constraint.is_hard,
                                source=constraint.source.value,
                                valid_until=(
                                    None
                                    if constraint.valid_until is None
                                    else self._db_time(constraint.valid_until)
                                ),
                                created_at=self._db_time(constraint.created_at),
                                version=constraint.version,
                            )
                        )
                    draft.status = ProfileDraftStatus.APPLIED.value
                    draft.applied_at = self._db_time(now)
                    draft.applied_profile_id = str(merge.profile.id)
                    draft.apply_request_id = result.client_request_id
                    draft.apply_fingerprint = result.apply_fingerprint
                    draft.apply_result = self._apply_result_payload(result)
                    draft.updated_at = self._db_time(now)
                    draft.version += 1
                    self._audit(
                        session,
                        user_id,
                        "PROFILE_DRAFT_APPLIED",
                        {"draft_id": draft.id, "profile_id": str(merge.profile.id)},
                        now,
                    )
                    return ProfileDraftCommitResult(result=result, created=True)
            except IntegrityError as exc:
                raise ProfileStateConflictError(
                    "Profile Draft apply conflicted"
                ) from exc

    async def reject(
        self,
        *,
        draft_id: UUID,
        user_id: UUID,
        client_request_id: str,
        expected_draft_version: int,
        reject_fingerprint: str,
        now: datetime,
    ) -> ProfileDraftRejectResult:
        async with self._sessions() as session:
            try:
                async with session.begin():
                    existing = await session.scalar(
                        select(ProfileDraftModel)
                        .where(
                            ProfileDraftModel.user_id == str(user_id),
                            ProfileDraftModel.reject_request_id == client_request_id,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        if (
                            existing.id != str(draft_id)
                            or existing.reject_fingerprint != reject_fingerprint
                            or existing.rejected_at is None
                        ):
                            raise ProfileDraftRejectIdempotencyConflictError(
                                "reject request id belongs to another payload"
                            )
                        return ProfileDraftRejectResult(
                            draft_id=draft_id,
                            user_id=user_id,
                            client_request_id=client_request_id,
                            reject_fingerprint=reject_fingerprint,
                            rejected_at=self._utc(existing.rejected_at),
                            created=False,
                        )
                    draft = await self._locked_draft(session, draft_id, user_id)
                    self._validate_pending(draft, expected_draft_version, now)
                    draft.status = ProfileDraftStatus.REJECTED.value
                    draft.rejected_at = self._db_time(now)
                    draft.reject_request_id = client_request_id
                    draft.reject_fingerprint = reject_fingerprint
                    draft.updated_at = self._db_time(now)
                    draft.version += 1
                    self._audit(
                        session,
                        user_id,
                        "PROFILE_DRAFT_REJECTED",
                        {"draft_id": draft.id},
                        now,
                    )
                    return ProfileDraftRejectResult(
                        draft_id=draft_id,
                        user_id=user_id,
                        client_request_id=client_request_id,
                        reject_fingerprint=reject_fingerprint,
                        rejected_at=now,
                        created=True,
                    )
            except IntegrityError as exc:
                raise ProfileStateConflictError(
                    "Profile Draft reject conflicted"
                ) from exc

    async def _locked_draft(
        self, session: AsyncSession, draft_id: UUID, user_id: UUID
    ) -> ProfileDraftModel:
        row = await session.scalar(
            select(ProfileDraftModel)
            .where(
                ProfileDraftModel.id == str(draft_id),
                ProfileDraftModel.user_id == str(user_id),
            )
            .with_for_update()
        )
        if row is None:
            raise ProfileDraftNotFoundError("draft was not found")
        return row

    def _active_or_none(
        self, session: AsyncSession, row: ProfileDraftModel | None
    ) -> ProfileAgentDraft | None:
        if row is None:
            return None
        now = self._clock()
        if (
            row.status == ProfileDraftStatus.PENDING_REVIEW.value
            and self._utc(row.expires_at) <= now
        ):
            row.status = ProfileDraftStatus.EXPIRED.value
            row.updated_at = self._db_time(now)
            row.version += 1
            self._audit(
                session,
                UUID(row.user_id),
                "PROFILE_DRAFT_EXPIRED",
                {"draft_id": row.id},
                now,
            )
            return None
        return self._draft_from_row(row)

    def _validate_pending(
        self, draft: ProfileDraftModel, expected_version: int, now: datetime
    ) -> None:
        if draft.status == ProfileDraftStatus.APPLIED.value:
            raise ProfileDraftAlreadyAppliedError("draft is already applied")
        if draft.status == ProfileDraftStatus.REJECTED.value:
            raise ProfileDraftRejectedError("rejected draft cannot be applied")
        if (
            draft.context_snapshot_id is None
            or draft.context_fingerprint is None
            or draft.context_contract_version is None
            or draft.context_policy_version is None
        ):
            raise ProfileStateConflictError(
                "draft context snapshot reference is incomplete"
            )
        if (
            draft.status == ProfileDraftStatus.EXPIRED.value
            or self._utc(draft.expires_at) <= now
        ):
            raise ProfileDraftExpiredError("expired draft cannot be applied")
        if draft.version != expected_version:
            raise ProfileDraftVersionConflictError("draft version changed")

    def _apply_profile(
        self,
        row: FitnessProfileModel | None,
        merge: ProfileDraftMergeOutcome,
        expected_version: int | None,
    ) -> None:
        profile = merge.profile
        if row is None:
            if expected_version is not None or profile.version != 1:
                raise ProfileVersionConflictError("new profile version changed")
            return
        if (
            expected_version != row.version
            or str(profile.id) != row.id
            or profile.version != row.version + 1
        ):
            raise ProfileVersionConflictError("profile version changed")
        row.experience_level = profile.experience_level.value
        row.primary_goal = profile.primary_goal.value
        row.weekly_frequency = profile.weekly_frequency
        row.max_session_minutes = profile.max_session_minutes
        row.scope_confirmed = profile.scope_confirmed
        row.updated_at = self._db_time(profile.updated_at)
        row.version = profile.version

    def _profile_row(self, merge: ProfileDraftMergeOutcome) -> FitnessProfileModel:
        profile = merge.profile
        return FitnessProfileModel(
            id=str(profile.id),
            user_id=str(profile.user_id),
            experience_level=profile.experience_level.value,
            primary_goal=profile.primary_goal.value,
            weekly_frequency=profile.weekly_frequency,
            max_session_minutes=profile.max_session_minutes,
            scope_confirmed=profile.scope_confirmed,
            created_at=self._db_time(profile.created_at),
            updated_at=self._db_time(profile.updated_at),
            version=profile.version,
        )

    def _audit(
        self,
        session: AsyncSession,
        user_id: UUID,
        event_type: str,
        metadata: dict[str, str],
        now: datetime,
    ) -> None:
        session.add(
            AuditEventModel(
                id=str(uuid4()),
                user_id=str(user_id),
                run_id=None,
                step_id=None,
                sequence_no=None,
                event_type=event_type,
                event_metadata=metadata,
                occurred_at=self._db_time(now),
            )
        )

    @classmethod
    def _row_from_draft(cls, draft: ProfileAgentDraft) -> ProfileDraftModel:
        return ProfileDraftModel(
            id=str(draft.id),
            user_id=str(draft.user_id),
            request_id=str(draft.request_id),
            client_request_id=draft.client_request_id,
            request_payload_fingerprint=draft.request_payload_fingerprint,
            input_fingerprint=draft.input_fingerprint,
            output=draft.output.model_dump(mode="json"),
            prompt_version=draft.prompt_version,
            provider_summary=draft.provider_summary,
            fallback_used=draft.fallback_used,
            fallback_type=None
            if draft.fallback_type is None
            else draft.fallback_type.value,
            status=draft.status.value,
            context_snapshot_id=(
                None
                if draft.context_snapshot_reference_id is None
                else str(draft.context_snapshot_reference_id)
            ),
            context_fingerprint=draft.context_fingerprint,
            context_contract_version=draft.context_contract_version,
            context_policy_version=draft.context_policy_version,
            context_degraded_mode=draft.context_degraded_mode.value,
            context_included_memory_count=draft.context_included_memory_count,
            expires_at=cls._db_time(draft.expires_at),
            applied_at=None
            if draft.applied_at is None
            else cls._db_time(draft.applied_at),
            rejected_at=None
            if draft.rejected_at is None
            else cls._db_time(draft.rejected_at),
            applied_profile_id=None
            if draft.applied_profile_id is None
            else str(draft.applied_profile_id),
            apply_request_id=draft.apply_request_id,
            apply_fingerprint=None,
            apply_result=None,
            reject_request_id=None,
            reject_fingerprint=None,
            created_at=cls._db_time(draft.created_at),
            updated_at=cls._db_time(draft.created_at),
            version=draft.version,
        )

    @classmethod
    def _draft_from_row(cls, row: ProfileDraftModel) -> ProfileAgentDraft:
        from app.domain.context.enums import ContextDegradedMode

        return ProfileAgentDraft(
            id=UUID(row.id),
            request_id=UUID(row.request_id),
            client_request_id=row.client_request_id,
            user_id=UUID(row.user_id),
            request_payload_fingerprint=row.request_payload_fingerprint,
            input_fingerprint=row.input_fingerprint,
            output=ProfileAgentOutput.model_validate(row.output),
            prompt_version=row.prompt_version,
            provider_summary=row.provider_summary,
            fallback_used=row.fallback_used,
            fallback_type=None
            if row.fallback_type is None
            else FallbackType(row.fallback_type),
            created_at=cls._utc(row.created_at),
            expires_at=cls._utc(row.expires_at),
            status=ProfileDraftStatus(row.status),
            version=row.version,
            applied_at=None if row.applied_at is None else cls._utc(row.applied_at),
            rejected_at=None if row.rejected_at is None else cls._utc(row.rejected_at),
            applied_profile_id=None
            if row.applied_profile_id is None
            else UUID(row.applied_profile_id),
            apply_request_id=row.apply_request_id,
            context_snapshot_reference_id=None
            if row.context_snapshot_id is None
            else UUID(row.context_snapshot_id),
            context_fingerprint=row.context_fingerprint,
            context_contract_version=row.context_contract_version,
            context_policy_version=row.context_policy_version,
            context_degraded_mode=ContextDegradedMode(row.context_degraded_mode),
            context_included_memory_count=row.context_included_memory_count,
        )

    @staticmethod
    def _apply_result_payload(value: ProfileDraftApplyResult) -> dict[str, Any]:
        return {
            "id": str(value.id),
            "user_id": str(value.user_id),
            "draft_id": str(value.draft_id),
            "client_request_id": value.client_request_id,
            "apply_fingerprint": value.apply_fingerprint,
            "profile_id": str(value.profile_id),
            "previous_profile_version": value.previous_profile_version,
            "resulting_profile_version": value.resulting_profile_version,
            "added_constraint_ids": [str(item) for item in value.added_constraint_ids],
            "unchanged_constraint_ids": [
                str(item) for item in value.unchanged_constraint_ids
            ],
            "ignored_soft_preference_count": value.ignored_soft_preference_count,
            "ignored_memory_candidate_count": value.ignored_memory_candidate_count,
            "applied_at": value.applied_at.isoformat(),
        }

    @staticmethod
    def _apply_result_from_payload(payload: dict[str, Any]) -> ProfileDraftApplyResult:
        return ProfileDraftApplyResult(
            id=UUID(payload["id"]),
            user_id=UUID(payload["user_id"]),
            draft_id=UUID(payload["draft_id"]),
            client_request_id=str(payload["client_request_id"]),
            apply_fingerprint=str(payload["apply_fingerprint"]),
            profile_id=UUID(payload["profile_id"]),
            previous_profile_version=payload["previous_profile_version"],
            resulting_profile_version=int(payload["resulting_profile_version"]),
            added_constraint_ids=tuple(
                UUID(item) for item in payload["added_constraint_ids"]
            ),
            unchanged_constraint_ids=tuple(
                UUID(item) for item in payload["unchanged_constraint_ids"]
            ),
            ignored_soft_preference_count=int(payload["ignored_soft_preference_count"]),
            ignored_memory_candidate_count=int(
                payload["ignored_memory_candidate_count"]
            ),
            applied_at=datetime.fromisoformat(str(payload["applied_at"])).astimezone(
                UTC
            ),
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    @staticmethod
    def _db_time(value: datetime) -> datetime:
        return value.astimezone(UTC).replace(tzinfo=None)
