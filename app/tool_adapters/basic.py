"""Safe deterministic adapters; they do not perform retry or persistence."""

import hashlib
import json
from uuid import UUID

from pydantic import BaseModel

from app.calendar_operations.gateway import CalendarProviderError
from app.calendar_read.gateway import CalendarReadError
from app.domain.calendar_operations.protocols import CalendarWriteProvider
from app.domain.calendar_read.models import CalendarReadRequest
from app.domain.calendar_read.protocols import CalendarReadProvider
from app.domain.common import LocationType
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.memory.errors import MemoryDomainError
from app.domain.tools.enums import ToolErrorCategory
from app.domain.tools.errors import ToolError
from app.domain.tools.models import ToolInvocationContext
from app.ics.builder import IcsBuilder
from app.memory.candidate_service import MemoryCandidateService
from app.recovery.spacing_validator import RecoverySpacingValidator
from app.session_design.duration import SessionDurationPolicy
from app.tool_adapters.contracts import (
    CalendarCommitRequest,
    CalendarCommitResponse,
    CalendarFreeBusyRequest,
    CalendarFreeBusyResponse,
    ExerciseCatalogSearchRequest,
    ExerciseCatalogSearchResponse,
    ExerciseToolView,
    IcsExportRequest,
    IcsExportResponse,
    MemoryCandidateCreateRequest,
    MemoryCandidateCreateResponse,
    RecoverySpacingRequest,
    RecoverySpacingResponse,
    ReferenceResponse,
    SessionDurationRequest,
    SessionDurationResponse,
)


class CatalogSearchAdapter:
    def __init__(self, repository: ExerciseRepository | None) -> None:
        self._repository = repository
        self.invocation_count = 0

    async def invoke_once(
        self, context: ToolInvocationContext, request: ExerciseCatalogSearchRequest
    ) -> ExerciseCatalogSearchResponse:
        self.invocation_count += 1
        if self._repository is None:
            values = []
        else:
            location = (
                None
                if not request.allowed_locations
                else LocationType(request.allowed_locations[0])
            )
            equipment = (
                None
                if not request.available_equipment
                else set(request.available_equipment)
            )
            values = await self._repository.list_active(location, equipment)
        filtered = [
            item
            for item in values
            if not (item.feature_tags & set(request.excluded_features))
            and (
                not request.allowed_difficulties
                or item.difficulty_level.value in request.allowed_difficulties
            )
            and (
                not request.required_movement_patterns
                or bool(
                    item.movement_patterns & set(request.required_movement_patterns)
                )
            )
        ]
        views = tuple(
            ExerciseToolView(
                exercise_id=item.id,
                name=item.name,
                difficulty=item.difficulty_level.value,
                supported_locations=tuple(sorted(x.value for x in item.location_types)),
                required_equipment=tuple(sorted(item.required_equipment)),
                movement_patterns=tuple(sorted(item.movement_patterns)),
                feature_tags=tuple(sorted(item.feature_tags)),
                default_duration_seconds=item.default_duration_seconds,
                status=item.status.value,
                version=item.version,
            )
            for item in sorted(filtered, key=lambda x: x.id)
        )
        payload = [item.model_dump(mode="json") for item in views]
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        catalog_version = hashlib.sha256(
            "|".join(f"{item.exercise_id}:{item.version}" for item in views).encode()
        ).hexdigest()
        return ExerciseCatalogSearchResponse(
            exercises=views,
            catalog_version=catalog_version,
            result_fingerprint=fingerprint,
        )

    async def close(self) -> None:
        return None


class SessionDurationAdapter:
    def __init__(self) -> None:
        self._policy = SessionDurationPolicy()
        self.invocation_count = 0

    async def invoke_once(
        self, context: ToolInvocationContext, request: SessionDurationRequest
    ) -> SessionDurationResponse:
        self.invocation_count += 1
        exercises, duration = self._policy.fit_exact(
            request.exercises, request.target_duration_minutes
        )
        return SessionDurationResponse(
            exercises=exercises,
            duration=duration,
            policy_version=self._policy.version,
        )

    async def close(self) -> None:
        return None


class RecoverySpacingAdapter:
    def __init__(self) -> None:
        self._validator = RecoverySpacingValidator()
        self.invocation_count = 0

    async def invoke_once(
        self, context: ToolInvocationContext, request: RecoverySpacingRequest
    ) -> RecoverySpacingResponse:
        self.invocation_count += 1
        result = self._validator.validate(plan=request.plan, selected=request.selected)
        return RecoverySpacingResponse(
            passed=result.passed,
            violation_codes=result.violation_codes,
            affected_session_ids=result.affected_session_ids,
        )

    async def close(self) -> None:
        return None


class DisabledCalendarReadAdapter:
    async def invoke_once(
        self, context: ToolInvocationContext, request: CalendarFreeBusyRequest
    ) -> CalendarFreeBusyResponse:
        raise ToolError(ToolErrorCategory.CONNECTION, "CALENDAR_READ_UNAVAILABLE")

    async def close(self) -> None:
        return None


class DisabledCalendarCommitAdapter:
    async def invoke_once(
        self, context: ToolInvocationContext, request: CalendarCommitRequest
    ) -> CalendarCommitResponse:
        raise ToolError(ToolErrorCategory.CONNECTION, "CALENDAR_WRITE_UNAVAILABLE")

    async def close(self) -> None:
        return None


class CalendarReadAdapter:
    """Single provider call; retry and degradation belong to Tool Gateway callers."""

    def __init__(self, provider: CalendarReadProvider | None) -> None:
        self._provider = provider
        self.invocation_count = 0

    async def invoke_once(
        self, context: ToolInvocationContext, request: CalendarFreeBusyRequest
    ) -> CalendarFreeBusyResponse:
        self.invocation_count += 1
        if self._provider is None:
            raise ToolError(ToolErrorCategory.CONNECTION, "CALENDAR_READ_DISABLED")
        try:
            values = await self._provider.read_busy(
                CalendarReadRequest(
                    start=request.start, end=request.end, timezone=request.timezone
                )
            )
        except CalendarReadError as exc:
            category = (
                ToolErrorCategory.CONNECTION
                if exc.retryable
                else ToolErrorCategory.BUSINESS_REJECTED
            )
            raise ToolError(category, "CALENDAR_READ_PROVIDER_FAILED") from exc
        return CalendarFreeBusyResponse(
            busy=tuple({"start": item.start, "end": item.end} for item in values)
        )

    async def close(self) -> None:
        return None


class CalendarCommitAdapter:
    """Single external provider attempt retaining the executor's operation key."""

    def __init__(self, provider: CalendarWriteProvider | None) -> None:
        self._provider = provider
        self.invocation_count = 0

    async def invoke_once(
        self, context: ToolInvocationContext, request: CalendarCommitRequest
    ) -> CalendarCommitResponse:
        self.invocation_count += 1
        if self._provider is None:
            raise ToolError(ToolErrorCategory.CONNECTION, "CALENDAR_WRITE_DISABLED")
        try:
            if request.operation == "CREATE" and request.payload is not None:
                result = await self._provider.create_event(
                    calendar_id=request.calendar_id,
                    operation_key=request.operation_key,
                    payload=request.payload,
                )
            elif (
                request.operation == "UPDATE"
                and request.payload is not None
                and request.external_event_id
            ):
                result = await self._provider.update_event(
                    calendar_id=request.calendar_id,
                    external_event_id=request.external_event_id,
                    operation_key=request.operation_key,
                    payload=request.payload,
                )
            elif request.operation == "DELETE" and request.external_event_id:
                result = await self._provider.delete_event(
                    calendar_id=request.calendar_id,
                    external_event_id=request.external_event_id,
                    operation_key=request.operation_key,
                )
            else:
                raise ToolError(
                    ToolErrorCategory.INVALID_REQUEST,
                    "CALENDAR_COMMIT_REQUEST_INVALID",
                )
        except CalendarProviderError as exc:
            category = (
                ToolErrorCategory.CONNECTION
                if exc.retryable
                else ToolErrorCategory.BUSINESS_REJECTED
            )
            raise ToolError(category, exc.code) from exc
        return CalendarCommitResponse(
            external_event_id=result.external_event_id,
            response_reference=result.response_reference_hash,
        )

    async def close(self) -> None:
        return None


class ReferenceAdapter:
    def __init__(self, reference: str) -> None:
        self._reference = reference

    async def invoke_once(
        self, context: ToolInvocationContext, request: BaseModel
    ) -> ReferenceResponse:
        return ReferenceResponse(reference=self._reference)

    async def close(self) -> None:
        return None


class IcsExportAdapter:
    """One deterministic builder call; the Gateway owns invocation policy."""

    def __init__(self, builder: IcsBuilder | None = None) -> None:
        self._builder = builder or IcsBuilder()
        self.invocation_count = 0

    async def invoke_once(
        self, context: ToolInvocationContext, request: IcsExportRequest
    ) -> IcsExportResponse:
        self.invocation_count += 1
        content, event_count = self._builder.build(
            user_id=UUID(request.user_id), plan=request.plan, as_of=request.as_of
        )
        return IcsExportResponse(content=content, event_count=event_count)

    async def close(self) -> None:
        return None


class MemoryCandidateCreateAdapter:
    def __init__(self, service: MemoryCandidateService | None) -> None:
        self._service = service
        self.invocation_count = 0

    async def invoke_once(
        self, context: ToolInvocationContext, request: MemoryCandidateCreateRequest
    ) -> MemoryCandidateCreateResponse:
        self.invocation_count += 1
        if self._service is None:
            raise ToolError(
                ToolErrorCategory.INTERNAL_ERROR,
                "MEMORY_CANDIDATE_SERVICE_UNAVAILABLE",
            )
        try:
            outcome = await self._service.create_candidate(
                request.user_id, request.command
            )
        except MemoryDomainError as exc:
            raise ToolError(ToolErrorCategory.BUSINESS_REJECTED, exc.code) from exc
        candidate = outcome.candidate
        result_fingerprint = hashlib.sha256(
            (
                f"{candidate.id}:{candidate.status.value}:{candidate.version}:"
                f"{not outcome.created}"
            ).encode()
        ).hexdigest()
        return MemoryCandidateCreateResponse(
            candidate_id=candidate.id,
            status=candidate.status,
            version=candidate.version,
            idempotent_reuse=not outcome.created,
            result_fingerprint=result_fingerprint,
        )

    async def close(self) -> None:
        return None
