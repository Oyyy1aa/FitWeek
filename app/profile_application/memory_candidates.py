"""Review and import Profile Draft proposals without auto-activating Memory."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from app.application.memories import MemoryApplicationService
from app.domain.common import utc_now
from app.domain.memory.enums import MemorySource, MemoryType
from app.domain.memory.errors import (
    DraftMemoryCandidateConflictError,
    DraftMemoryCandidateImportIdempotencyConflictError,
    DraftMemoryCandidateIndexInvalidError,
    DraftMemoryCandidateUnsupportedError,
    MemoryInvalidValueError,
)
from app.domain.profile_agent.memory_candidates import (
    DraftMemoryCandidateImportRepository,
    DraftMemoryCandidateImportResult,
    DraftMemoryCandidatePreview,
    DraftMemoryCandidatePreviewItem,
    ImportDraftMemoryCandidatesCommand,
)
from app.domain.profile_agent.models import (
    ProfileAgentDraft,
    ProfileDraftStatus,
    ScopeStatus,
)
from app.domain.profile_agent.repositories import ProfileAgentDraftRepository
from app.domain.users.models import UserAccount
from app.memory.candidate_service import CreateCandidateCommand
from app.memory.metrics import MemoryMetrics
from app.memory.normalization import fingerprint, normalize_display


class ProfileDraftMemoryCandidateService:
    def __init__(
        self,
        *,
        drafts: ProfileAgentDraftRepository,
        memories: MemoryApplicationService,
        imports: DraftMemoryCandidateImportRepository,
        metrics: MemoryMetrics,
    ) -> None:
        self._drafts = drafts
        self._memories = memories
        self._imports = imports
        self._metrics = metrics

    async def preview(
        self,
        *,
        user: UserAccount,
        draft_id: UUID,
        selected_candidate_indexes: tuple[int, ...],
    ) -> DraftMemoryCandidatePreview:
        draft = await self._require_draft(user, draft_id)
        indexes = self._indexes(
            selected_candidate_indexes, len(draft.output.memory_candidates)
        )
        items = tuple(
            self._preview_item(
                draft_id, index, draft.output.memory_candidates[index].value
            )
            for index in indexes
        )
        self._metrics.increment("draft_memory_candidate_previews")
        return DraftMemoryCandidatePreview(
            draft_id=draft.id,
            draft_version=draft.version,
            items=items,
        )

    async def import_candidates(
        self,
        *,
        user: UserAccount,
        draft_id: UUID,
        command: ImportDraftMemoryCandidatesCommand,
    ) -> DraftMemoryCandidateImportResult:
        draft = await self._require_draft(user, draft_id)
        if draft.version != command.expected_draft_version:
            raise DraftMemoryCandidateImportIdempotencyConflictError(
                "The Draft version no longer matches the import decision."
            )
        indexes = self._indexes(
            command.selected_candidate_indexes,
            len(draft.output.memory_candidates),
        )
        request_id = command.client_request_id.strip()
        import_fingerprint = fingerprint(
            {
                "user_id": str(user.id),
                "draft_id": str(draft_id),
                "draft_version": command.expected_draft_version,
                "indexes": list(indexes),
                "policy": "draft-memory-candidate-v1",
            }
        )
        existing = await self._imports.get(user.id, request_id)
        if existing is not None:
            if existing[0] != import_fingerprint:
                self._metrics.increment("draft_memory_candidate_import_conflicts")
                raise DraftMemoryCandidateImportIdempotencyConflictError(
                    "The import request ID was used with another selection."
                )
            return replace(existing[1], created=False)
        preview = await self.preview(
            user=user,
            draft_id=draft_id,
            selected_candidate_indexes=indexes,
        )
        unsupported = [item for item in preview.items if not item.supported]
        if unsupported:
            raise DraftMemoryCandidateUnsupportedError(
                unsupported[0].rejection_reason
                or "A selected Draft proposal is not a supported Memory type."
            )
        candidate_ids: list[UUID] = []
        statuses = []
        for item in preview.items:
            assert item.memory_type is not None and item.proposed_key is not None
            outcome = await self._memories.create_candidate(
                user,
                CreateCandidateCommand(
                    client_request_id=(
                        f"draft-import:{draft_id}:{item.proposal_index}"
                    ),
                    memory_type=item.memory_type,
                    key=item.proposed_key,
                    value=item.proposed_value,
                    source=MemorySource.PROFILE_AGENT_CANDIDATE,
                    source_reference=item.source_reference,
                    evidence_summary=item.evidence_summary,
                    confidence=Decimal("0.75"),
                    expires_at=min(draft.expires_at, utc_now() + timedelta(days=7)),
                ),
            )
            candidate_ids.append(outcome.candidate.id)
            statuses.append(outcome.candidate.status)
        result = DraftMemoryCandidateImportResult(
            draft_id=draft_id,
            client_request_id=request_id,
            import_fingerprint=import_fingerprint,
            candidate_ids=tuple(candidate_ids),
            statuses=tuple(statuses),
            created=True,
        )
        saved = await self._imports.save(
            user.id, request_id, import_fingerprint, result
        )
        self._metrics.increment("draft_memory_candidates_imported", len(candidate_ids))
        return saved

    async def _require_draft(
        self, user: UserAccount, draft_id: UUID
    ) -> ProfileAgentDraft:
        draft = await self._drafts.get(draft_id, user.id)
        if draft is None:
            raise DraftMemoryCandidateConflictError(
                "The Profile Draft is missing, expired, or belongs to another user."
            )
        if draft.output.scope_status is not ScopeStatus.SUPPORTED:
            raise DraftMemoryCandidateUnsupportedError(
                "Out-of-scope Draft proposals cannot become Memory Candidates."
            )
        if draft.status in {ProfileDraftStatus.REJECTED, ProfileDraftStatus.EXPIRED}:
            raise DraftMemoryCandidateConflictError(
                "A rejected or expired Draft cannot import Memory Candidates."
            )
        return draft

    @staticmethod
    def _indexes(values: tuple[int, ...], length: int) -> tuple[int, ...]:
        if not values or len(values) != len(set(values)):
            raise DraftMemoryCandidateIndexInvalidError(
                "Select one or more unique Draft proposal indexes."
            )
        if any(value < 0 or value >= length for value in values):
            raise DraftMemoryCandidateIndexInvalidError(
                "A selected Draft proposal index is outside the Draft."
            )
        return tuple(sorted(values))

    @staticmethod
    def _preview_item(
        draft_id: UUID, index: int, raw_value: str
    ) -> DraftMemoryCandidatePreviewItem:
        try:
            value = normalize_display(raw_value, maximum=120)
        except MemoryInvalidValueError as exc:
            return DraftMemoryCandidatePreviewItem(
                proposal_index=index,
                memory_type=None,
                proposed_key=None,
                proposed_value="[rejected]",
                source_reference=f"profile-draft:{draft_id}:proposal:{index}",
                evidence_summary=f"Selected Profile Draft proposal {index}.",
                supported=False,
                rejection_reason=str(exc),
            )
        lowered = value.casefold()
        mapping: tuple[tuple[tuple[str, ...], MemoryType, str], ...] = (
            (
                ("morning", "afternoon", "evening"),
                MemoryType.PREFERRED_TIME_OF_DAY,
                "preferred_time_of_day",
            ),
            (
                ("home", "gym", "outdoor"),
                MemoryType.PREFERRED_LOCATION,
                "preferred_location",
            ),
            (
                ("dumbbell", "resistance band", "yoga mat"),
                MemoryType.PREFERRED_EQUIPMENT,
                "preferred_equipment",
            ),
            (
                ("running", "jumping", "burpee"),
                MemoryType.DISLIKED_ACTIVITY,
                "disliked_activity",
            ),
        )
        for markers, memory_type, key in mapping:
            matched = next((marker for marker in markers if marker in lowered), None)
            if matched is not None:
                return DraftMemoryCandidatePreviewItem(
                    proposal_index=index,
                    memory_type=memory_type,
                    proposed_key=key,
                    proposed_value=matched,
                    source_reference=f"profile-draft:{draft_id}:proposal:{index}",
                    evidence_summary=f"Selected Profile Draft proposal {index}.",
                    supported=True,
                )
        return DraftMemoryCandidatePreviewItem(
            proposal_index=index,
            memory_type=None,
            proposed_key=None,
            proposed_value=value,
            source_reference=f"profile-draft:{draft_id}:proposal:{index}",
            evidence_summary=f"Selected Profile Draft proposal {index}.",
            supported=False,
            rejection_reason="Proposal does not map to a controlled Memory type.",
        )
