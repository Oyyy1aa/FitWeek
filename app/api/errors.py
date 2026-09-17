"""Stable HTTP error envelopes for application failures."""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.application.errors import (
    ApplicationError,
    BusinessRuleViolation,
    CalendarOperationExecutionFailed,
    ConflictError,
    LocalReplanningFailed,
    PlanGenerationFailed,
    RecoveryPlanApplicationFailed,
    ResourceNotFound,
    SessionDesignPlanApplicationFailed,
    UnsupportedPersistenceBackend,
)
from app.domain.common import DomainValidationError
from app.domain.memory.errors import (
    ContextBudgetExceededError,
    ContextContractNotFoundError,
    ContextDebugApiDisabledError,
    ContextSnapshotNotFoundError,
    ContextSnapshotVersionMismatchError,
    DraftMemoryCandidateAlreadyImportedError,
    DraftMemoryCandidateConflictError,
    DraftMemoryCandidateImportIdempotencyConflictError,
    DraftMemoryCandidateIndexInvalidError,
    DraftMemoryCandidateUnsupportedError,
    MemoryCandidateAlreadyAcceptedError,
    MemoryCandidateExpiredError,
    MemoryCandidateIdempotencyConflictError,
    MemoryCandidateNotFoundError,
    MemoryCandidateRejectedError,
    MemoryCandidateVersionConflictError,
    MemoryConflictError,
    MemoryDomainError,
    MemoryEvidenceRequiredError,
    MemoryIdempotencyConflictError,
    MemoryInvalidTypeError,
    MemoryInvalidValueError,
    MemoryNotActiveError,
    MemoryNotFoundError,
    MemoryQueryFailedError,
    MemoryVersionConflictError,
)
from app.safety.models import SafetyViolation


def _violation_payload(violation: SafetyViolation) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": violation.code,
        "message": violation.message,
    }
    for field in ("path", "session_id", "exercise_id"):
        value = getattr(violation, field, None)
        if value is not None:
            payload[field] = str(value)
    return payload


async def _application_error_handler(
    _: Request,
    exc: Exception,
) -> JSONResponse:
    if not isinstance(exc, ApplicationError):
        raise exc
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    if isinstance(exc, ResourceNotFound):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, ConflictError):
        http_status = status.HTTP_409_CONFLICT
    elif isinstance(exc, UnsupportedPersistenceBackend):
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(
        exc,
        (
            SessionDesignPlanApplicationFailed,
            CalendarOperationExecutionFailed,
            RecoveryPlanApplicationFailed,
        ),
    ):
        http_status = status.HTTP_500_INTERNAL_SERVER_ERROR

    error: dict[str, object] = {"code": exc.code, "message": str(exc)}
    request_id = getattr(exc, "request_id", None)
    if request_id is not None:
        error["request_id"] = str(request_id)
    if isinstance(exc, BusinessRuleViolation) and exc.violations:
        error["violations"] = [
            _violation_payload(violation) for violation in exc.violations
        ]
    if isinstance(exc, PlanGenerationFailed):
        error["reasons"] = [
            {"code": reason.code, "message": reason.message} for reason in exc.reasons
        ]
    if isinstance(exc, LocalReplanningFailed):
        error["reasons"] = [
            {
                "code": reason.code,
                "message": reason.message,
                **(
                    {"session_id": str(reason.session_id)}
                    if reason.session_id is not None
                    else {}
                ),
            }
            for reason in exc.reasons
        ]
    return JSONResponse(status_code=http_status, content={"error": error})


async def _request_validation_error_handler(
    _: Request,
    exc: Exception,
) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    violations = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        violation: dict[str, object] = {
            "code": "INVALID_REQUEST",
            "message": str(item.get("msg", "Invalid request value.")),
        }
        if location:
            violation["path"] = location
        violations.append(violation)
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "REQUEST_VALIDATION_FAILED",
                "message": "The request payload is invalid.",
                "violations": violations,
            }
        },
    )


async def _domain_validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainValidationError):
        raise exc
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": exc.code,
                "message": str(exc),
            }
        },
    )


async def _memory_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, MemoryDomainError):
        raise exc
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    if isinstance(
        exc,
        (
            MemoryNotFoundError,
            MemoryCandidateNotFoundError,
            MemoryCandidateExpiredError,
            ContextDebugApiDisabledError,
            ContextSnapshotNotFoundError,
        ),
    ):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(
        exc,
        (
            MemoryVersionConflictError,
            MemoryIdempotencyConflictError,
            MemoryConflictError,
            MemoryNotActiveError,
            MemoryCandidateRejectedError,
            MemoryCandidateAlreadyAcceptedError,
            MemoryCandidateVersionConflictError,
            MemoryCandidateIdempotencyConflictError,
            ContextSnapshotVersionMismatchError,
            DraftMemoryCandidateImportIdempotencyConflictError,
            DraftMemoryCandidateAlreadyImportedError,
            DraftMemoryCandidateConflictError,
        ),
    ):
        http_status = status.HTTP_409_CONFLICT
    elif isinstance(exc, MemoryQueryFailedError):
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(exc, ContextContractNotFoundError):
        http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    elif isinstance(
        exc,
        (
            MemoryInvalidTypeError,
            MemoryInvalidValueError,
            MemoryEvidenceRequiredError,
            ContextBudgetExceededError,
            DraftMemoryCandidateIndexInvalidError,
            DraftMemoryCandidateUnsupportedError,
        ),
    ):
        http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


async def _http_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    code = (
        "METHOD_NOT_ALLOWED"
        if exc.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        else f"HTTP_{exc.status_code}"
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": str(exc.detail)}},
        headers=exc.headers,
    )


def register_error_handlers(application: FastAPI) -> None:
    """Register application failures without leaking internal tracebacks."""

    application.add_exception_handler(ApplicationError, _application_error_handler)
    application.add_exception_handler(
        DomainValidationError, _domain_validation_error_handler
    )
    application.add_exception_handler(MemoryDomainError, _memory_error_handler)
    application.add_exception_handler(
        RequestValidationError,
        _request_validation_error_handler,
    )
    application.add_exception_handler(StarletteHTTPException, _http_error_handler)
