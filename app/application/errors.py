"""Application-level failures independent of HTTP and persistence adapters."""

from collections.abc import Sequence

from app.domain.planning.models import GenerationFailureReason
from app.domain.replanning.models import ReplanningFailureReason
from app.safety.models import SafetyViolation


class ApplicationError(RuntimeError):
    """Base class for failures intentionally exposed through an API mapping."""

    code = "APPLICATION_ERROR"


class ResourceNotFound(ApplicationError):
    """Requested resource does not exist for the current user."""

    code = "RESOURCE_NOT_FOUND"


class CheckInNotFound(ResourceNotFound):
    code = "CHECK_IN_NOT_FOUND"


class RevisionNotFound(ResourceNotFound):
    code = "REVISION_NOT_FOUND"


class ConflictError(ApplicationError):
    """A uniqueness or optimistic-lock check failed."""

    code = "CONFLICT"


class SessionAlreadyCheckedIn(ConflictError):
    code = "SESSION_ALREADY_CHECKED_IN"


class BusinessRuleViolation(ApplicationError):
    """One or more deterministic business rules rejected a command."""

    code = "BUSINESS_RULE_VIOLATION"

    def __init__(
        self,
        message: str,
        *,
        violations: Sequence[SafetyViolation] = (),
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.violations = tuple(violations)
        if code is not None:
            self.code = code


class ScopeViolation(BusinessRuleViolation):
    """The request is outside the supported non-medical product scope."""

    code = "SCOPE_VIOLATION"


class PlanGenerationFailed(ApplicationError):
    """A deterministic generator could not build a safe complete plan."""

    code = "PLAN_GENERATION_FAILED"

    def __init__(self, reasons: Sequence[GenerationFailureReason]) -> None:
        super().__init__("A safe weekly plan could not be generated.")
        self.reasons = tuple(reasons)


class LocalReplanningFailed(ApplicationError):
    """A deterministic local revision could not be safely constructed."""

    code = "LOCAL_REPLANNING_FAILED"

    def __init__(self, reasons: Sequence[ReplanningFailureReason]) -> None:
        super().__init__("A safe local plan revision could not be created.")
        self.reasons = tuple(reasons)


class IdempotencyConflict(ConflictError):
    code = "IDEMPOTENCY_CONFLICT"


class PlanVersionConflict(ConflictError):
    code = "PLAN_VERSION_CONFLICT"


class PlanGenerationIdempotencyConflict(ConflictError):
    code = "PLAN_GENERATION_IDEMPOTENCY_CONFLICT"


class RevisionConfirmationConflict(ConflictError):
    code = "REVISION_CONFIRMATION_CONFLICT"


class InvalidStateTransition(ConflictError):
    """An aggregate cannot move to the requested state."""

    code = "INVALID_STATE_TRANSITION"


class UnsupportedPersistenceBackend(ApplicationError):
    """Business repositories are unavailable for the selected backend."""

    code = "UNSUPPORTED_PERSISTENCE_BACKEND"


class RunNotFound(ResourceNotFound):
    code = "RUN_NOT_FOUND"


class RunIdempotencyConflict(ConflictError):
    code = "RUN_IDEMPOTENCY_CONFLICT"


class RunNotWaitingConfirmation(ConflictError):
    code = "RUN_NOT_WAITING_CONFIRMATION"


class RunAlreadyTerminal(ConflictError):
    code = "RUN_ALREADY_TERMINAL"


class RunConfirmationConflict(ConflictError):
    code = "RUN_CONFIRMATION_CONFLICT"


class OrchestratorDisabled(UnsupportedPersistenceBackend):
    code = "ORCHESTRATOR_DISABLED"


class ProfileAgentIdempotencyConflict(ConflictError):
    code = "PROFILE_AGENT_IDEMPOTENCY_CONFLICT"


class ProfileRequestOutOfScope(BusinessRuleViolation):
    code = "PROFILE_REQUEST_OUT_OF_SCOPE"

    def __init__(self, message: str, *, request_id: object) -> None:
        super().__init__(message)
        self.request_id = request_id


class ModelGatewayUnavailable(UnsupportedPersistenceBackend):
    code = "MODEL_GATEWAY_UNAVAILABLE"


class ProfileDraftNotFound(ResourceNotFound):
    code = "PROFILE_DRAFT_NOT_FOUND"


class ProfileDraftExpired(ResourceNotFound):
    code = "PROFILE_DRAFT_EXPIRED"


class ProfileDraftAlreadyApplied(ConflictError):
    code = "PROFILE_DRAFT_ALREADY_APPLIED"


class ProfileDraftRejected(ConflictError):
    code = "PROFILE_DRAFT_REJECTED"


class ProfileDraftVersionConflict(ConflictError):
    code = "PROFILE_DRAFT_VERSION_CONFLICT"


class ProfileVersionConflict(ConflictError):
    code = "PROFILE_VERSION_CONFLICT"


class ProfileDraftApplyIdempotencyConflict(ConflictError):
    code = "PROFILE_DRAFT_APPLY_IDEMPOTENCY_CONFLICT"


class ProfileDraftRejectIdempotencyConflict(ConflictError):
    code = "PROFILE_DRAFT_REJECT_IDEMPOTENCY_CONFLICT"


class ProfileDraftNotSupported(BusinessRuleViolation):
    code = "PROFILE_DRAFT_NOT_SUPPORTED"


class ProfileDraftApplyFailed(BusinessRuleViolation):
    code = "PROFILE_DRAFT_APPLY_FAILED"


class ProfileRunNotWaitingReview(ConflictError):
    code = "PROFILE_RUN_NOT_WAITING_REVIEW"


class SessionDesignNotFound(ResourceNotFound):
    code = "SESSION_DESIGN_NOT_FOUND"


class SessionDesignUnavailable(BusinessRuleViolation):
    code = "SESSION_DESIGN_UNAVAILABLE"


class SessionDesignIdempotencyConflict(ConflictError):
    code = "SESSION_DESIGN_IDEMPOTENCY_CONFLICT"


class SessionDesignReviewConflict(ConflictError):
    code = "SESSION_DESIGN_REVIEW_CONFLICT"


class SessionDesignDraftNotAccepted(ConflictError):
    code = "SESSION_DESIGN_DRAFT_NOT_ACCEPTED"


class SessionDesignDraftAlreadyApplied(ConflictError):
    code = "SESSION_DESIGN_DRAFT_ALREADY_APPLIED"


class SessionDesignDraftExpired(ResourceNotFound):
    code = "SESSION_DESIGN_DRAFT_EXPIRED"


class SessionDesignDraftVersionConflict(ConflictError):
    code = "SESSION_DESIGN_DRAFT_VERSION_CONFLICT"


class SessionDesignApplicationNotFound(ResourceNotFound):
    code = "SESSION_DESIGN_APPLICATION_NOT_FOUND"


class SessionDesignApplyIdempotencyConflict(ConflictError):
    code = "SESSION_DESIGN_APPLY_IDEMPOTENCY_CONFLICT"


class SessionDesignTargetPlanNotFound(ResourceNotFound):
    code = "SESSION_DESIGN_TARGET_PLAN_NOT_FOUND"


class SessionDesignSourceRevisionNotCurrent(ConflictError):
    code = "SESSION_DESIGN_SOURCE_REVISION_NOT_CURRENT"


class SessionDesignPlanVersionConflict(ConflictError):
    code = "SESSION_DESIGN_PLAN_VERSION_CONFLICT"


class SessionDesignTargetSessionNotFound(ResourceNotFound):
    code = "SESSION_DESIGN_TARGET_SESSION_NOT_FOUND"


class SessionDesignTargetSessionImmutable(BusinessRuleViolation):
    code = "SESSION_DESIGN_TARGET_SESSION_IMMUTABLE"


class SessionDesignTargetSessionCheckedIn(BusinessRuleViolation):
    code = "SESSION_DESIGN_TARGET_SESSION_CHECKED_IN"


class SessionDesignTargetDurationMismatch(BusinessRuleViolation):
    code = "SESSION_DESIGN_TARGET_DURATION_MISMATCH"


class SessionDesignTargetLocationMismatch(BusinessRuleViolation):
    code = "SESSION_DESIGN_TARGET_LOCATION_MISMATCH"


class SessionDesignTargetTypeMismatch(BusinessRuleViolation):
    code = "SESSION_DESIGN_TARGET_TYPE_MISMATCH"


class SessionDesignPlanSafetyFailed(BusinessRuleViolation):
    code = "SESSION_DESIGN_PLAN_SAFETY_FAILED"


class SessionDesignPlanApplicationFailed(ApplicationError):
    code = "SESSION_DESIGN_PLAN_APPLICATION_FAILED"


class SessionDesignRevisionConfirmationConflict(ConflictError):
    code = "SESSION_DESIGN_REVISION_CONFIRMATION_CONFLICT"


class ScheduleDraftNotFound(ResourceNotFound):
    code = "SCHEDULE_DRAFT_NOT_FOUND"


class ScheduleDraftExpired(ResourceNotFound):
    code = "SCHEDULE_DRAFT_EXPIRED"


class ScheduleDraftIdempotencyConflict(ConflictError):
    code = "SCHEDULE_DRAFT_IDEMPOTENCY_CONFLICT"


class ScheduleDraftVersionConflict(ConflictError):
    code = "SCHEDULE_DRAFT_VERSION_CONFLICT"


class ScheduleDraftReviewConflict(ConflictError):
    code = "SCHEDULE_DRAFT_REVIEW_CONFLICT"


class ScheduleSourceRevisionNotCurrent(ConflictError):
    code = "SCHEDULE_SOURCE_REVISION_NOT_CURRENT"


class SchedulePlanVersionConflict(ConflictError):
    code = "SCHEDULE_PLAN_VERSION_CONFLICT"


class ScheduleTargetSessionImmutable(BusinessRuleViolation):
    code = "SCHEDULE_TARGET_SESSION_IMMUTABLE"


class ScheduleDraftUnavailable(BusinessRuleViolation):
    code = "SCHEDULE_DRAFT_UNAVAILABLE"


class ScheduleSourcePlanNotFound(ResourceNotFound):
    code = "SCHEDULE_SOURCE_PLAN_NOT_FOUND"


class ScheduleTargetSessionNotFound(ResourceNotFound):
    code = "SCHEDULE_TARGET_SESSION_NOT_FOUND"


class ScheduleDraftNotAccepted(ConflictError):
    code = "SCHEDULE_DRAFT_NOT_ACCEPTED"


class ScheduleDraftIncomplete(BusinessRuleViolation):
    code = "SCHEDULE_DRAFT_INCOMPLETE"


class ScheduleDraftAlreadyApplied(ConflictError):
    code = "SCHEDULE_DRAFT_ALREADY_APPLIED"


class ScheduleApplicationNotFound(ResourceNotFound):
    code = "SCHEDULE_APPLICATION_NOT_FOUND"


class ScheduleApplyIdempotencyConflict(ConflictError):
    code = "SCHEDULE_APPLY_IDEMPOTENCY_CONFLICT"


class ScheduleTargetSessionCheckedIn(BusinessRuleViolation):
    code = "SCHEDULE_TARGET_SESSION_CHECKED_IN"


class ScheduleTargetSessionVersionConflict(ConflictError):
    code = "SCHEDULE_TARGET_SESSION_VERSION_CONFLICT"


class ScheduleBusySnapshotStale(ConflictError):
    code = "SCHEDULE_BUSY_SNAPSHOT_STALE"


class ScheduleNewCalendarConflict(BusinessRuleViolation):
    code = "SCHEDULE_NEW_CALENDAR_CONFLICT"


class ScheduleCalendarRevalidationFailed(ConflictError):
    code = "SCHEDULE_CALENDAR_REVALIDATION_FAILED"


class SchedulePlanSafetyFailed(BusinessRuleViolation):
    code = "SCHEDULE_PLAN_SAFETY_FAILED"


class SchedulePlanApplicationFailed(ApplicationError):
    code = "SCHEDULE_PLAN_APPLICATION_FAILED"


class IcsPlanNotConfirmed(ConflictError):
    code = "ICS_PLAN_NOT_CONFIRMED"


class IcsPlanRevisionNotCurrent(ConflictError):
    code = "ICS_PLAN_REVISION_NOT_CURRENT"


class IcsExportIdempotencyConflict(ConflictError):
    code = "ICS_EXPORT_IDEMPOTENCY_CONFLICT"


class IcsSerializationFailed(ApplicationError):
    code = "ICS_SERIALIZATION_FAILED"


class IcsValidationFailed(BusinessRuleViolation):
    code = "ICS_VALIDATION_FAILED"


class IcsExportNotFound(ResourceNotFound):
    code = "ICS_EXPORT_NOT_FOUND"


class CalendarOperationDraftNotFound(ResourceNotFound):
    code = "CALENDAR_OPERATION_DRAFT_NOT_FOUND"


class CalendarOperationIdempotencyConflict(ConflictError):
    code = "CALENDAR_OPERATION_IDEMPOTENCY_CONFLICT"


class CalendarOperationVersionConflict(ConflictError):
    code = "CALENDAR_OPERATION_VERSION_CONFLICT"


class CalendarOperationStateConflict(ConflictError):
    code = "CALENDAR_OPERATION_STATE_CONFLICT"


class CalendarOperationPlanNotConfirmed(ConflictError):
    code = "CALENDAR_OPERATION_PLAN_NOT_CONFIRMED"


class CalendarOperationPlanNotCurrent(ConflictError):
    code = "CALENDAR_OPERATION_PLAN_NOT_CURRENT"


class CalendarWriteDisabled(ConflictError):
    code = "CALENDAR_WRITE_DISABLED"


class CalendarOperationExecutionFailed(ApplicationError):
    code = "CALENDAR_OPERATION_EXECUTION_FAILED"


class RecoveryDraftNotFound(ResourceNotFound):
    code = "RECOVERY_DRAFT_NOT_FOUND"


class RecoverySourcePlanNotFound(ResourceNotFound):
    code = "RECOVERY_SOURCE_PLAN_NOT_FOUND"


class RecoverySourceRevisionNotCurrent(ConflictError):
    code = "RECOVERY_SOURCE_REVISION_NOT_CURRENT"


class RecoveryPlanVersionConflict(ConflictError):
    code = "RECOVERY_PLAN_VERSION_CONFLICT"


class RecoveryDraftIdempotencyConflict(ConflictError):
    code = "RECOVERY_DRAFT_IDEMPOTENCY_CONFLICT"


class RecoveryDraftVersionConflict(ConflictError):
    code = "RECOVERY_DRAFT_VERSION_CONFLICT"


class RecoveryDraftReviewConflict(ConflictError):
    code = "RECOVERY_DRAFT_REVIEW_CONFLICT"


class RecoveryDraftNotAcceptable(BusinessRuleViolation):
    code = "RECOVERY_DRAFT_NOT_ACCEPTABLE"


class RecoveryScopeOutOfScope(BusinessRuleViolation):
    code = "RECOVERY_SCOPE_OUT_OF_SCOPE"


class RecoveryScopeNeedsReview(BusinessRuleViolation):
    code = "RECOVERY_SCOPE_NEEDS_REVIEW"


class RecoveryTargetSessionNotFound(ResourceNotFound):
    code = "RECOVERY_TARGET_SESSION_NOT_FOUND"


class RecoveryTargetSessionImmutable(BusinessRuleViolation):
    code = "RECOVERY_TARGET_SESSION_IMMUTABLE"


class RecoveryNoSafeActionAvailable(BusinessRuleViolation):
    code = "RECOVERY_NO_SAFE_ACTION_AVAILABLE"


class RecoveryUnavailable(UnsupportedPersistenceBackend):
    code = "RECOVERY_UNAVAILABLE"


class RecoveryDraftNotAccepted(ConflictError):
    code = "RECOVERY_DRAFT_NOT_ACCEPTED"


class RecoveryDraftAlreadyApplied(ConflictError):
    code = "RECOVERY_DRAFT_ALREADY_APPLIED"


class RecoveryApplyIdempotencyConflict(ConflictError):
    code = "RECOVERY_APPLY_IDEMPOTENCY_CONFLICT"


class RecoveryInvalidActionSelection(BusinessRuleViolation):
    code = "RECOVERY_INVALID_ACTION_SELECTION"


class RecoveryConflictingActions(BusinessRuleViolation):
    code = "RECOVERY_CONFLICTING_ACTIONS"


class RecoverySubdraftReviewRequired(ConflictError):
    code = "RECOVERY_SUBDRAFT_REVIEW_REQUIRED"


class RecoverySubdraftRejected(ConflictError):
    code = "RECOVERY_SUBDRAFT_REJECTED"


class RecoverySubflowSnapshotNotFound(ResourceNotFound):
    code = "RECOVERY_SUBFLOW_SNAPSHOT_NOT_FOUND"


class RecoveryFrequencyViolation(BusinessRuleViolation):
    code = "RECOVERY_FREQUENCY_VIOLATION"


class RecoveryPlanSafetyFailed(BusinessRuleViolation):
    code = "RECOVERY_PLAN_SAFETY_FAILED"


class RecoveryPlanApplicationFailed(ApplicationError):
    code = "RECOVERY_PLAN_APPLICATION_FAILED"


class RecoveryApplicationNotFound(ResourceNotFound):
    code = "RECOVERY_APPLICATION_NOT_FOUND"


class RecoveryMemoryProposalInvalid(BusinessRuleViolation):
    code = "RECOVERY_MEMORY_PROPOSAL_INVALID"


class RecoveryMemoryProposalConflict(ConflictError):
    code = "RECOVERY_MEMORY_PROPOSAL_CONFLICT"


class RecoveryMemoryProposalIdempotencyConflict(ConflictError):
    code = "RECOVERY_MEMORY_PROPOSAL_IDEMPOTENCY_CONFLICT"
