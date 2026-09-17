"""Normalized model provider and contract failures."""

from app.domain.model_gateway.enums import ModelErrorCode


class ModelGatewayError(RuntimeError):
    """A safe, classified failure that can cross provider boundaries."""

    def __init__(
        self,
        code: ModelErrorCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ModelTimeoutError(ModelGatewayError):
    def __init__(self, message: str = "The model request timed out.") -> None:
        super().__init__(ModelErrorCode.MODEL_TIMEOUT, message, retryable=True)


class ModelConnectionError(ModelGatewayError):
    def __init__(
        self, message: str = "The model provider could not be reached."
    ) -> None:
        super().__init__(ModelErrorCode.MODEL_CONNECTION_ERROR, message, retryable=True)


class ModelRateLimitedError(ModelGatewayError):
    def __init__(
        self, message: str = "The model provider rate limit was reached."
    ) -> None:
        super().__init__(ModelErrorCode.MODEL_RATE_LIMITED, message, retryable=True)


class ModelServerError(ModelGatewayError):
    def __init__(
        self, message: str = "The model provider returned a server error."
    ) -> None:
        super().__init__(ModelErrorCode.MODEL_SERVER_ERROR, message, retryable=True)


class ModelAuthenticationError(ModelGatewayError):
    def __init__(
        self, message: str = "The model provider rejected authentication."
    ) -> None:
        super().__init__(
            ModelErrorCode.MODEL_AUTHENTICATION_ERROR,
            message,
            retryable=False,
        )


class ModelPermissionError(ModelGatewayError):
    def __init__(self, message: str = "The model provider denied the request.") -> None:
        super().__init__(
            ModelErrorCode.MODEL_PERMISSION_ERROR, message, retryable=False
        )


class ModelInvalidRequestError(ModelGatewayError):
    def __init__(self, message: str = "The model request was rejected.") -> None:
        super().__init__(ModelErrorCode.MODEL_INVALID_REQUEST, message, retryable=False)


class ModelResponseTooLargeError(ModelGatewayError):
    def __init__(
        self, message: str = "The model response exceeded the byte limit."
    ) -> None:
        super().__init__(
            ModelErrorCode.MODEL_RESPONSE_TOO_LARGE,
            message,
            retryable=False,
        )


class ModelEmptyResponseError(ModelGatewayError):
    def __init__(self, message: str = "The model response was empty.") -> None:
        super().__init__(ModelErrorCode.MODEL_EMPTY_RESPONSE, message, retryable=False)


class ModelInvalidJsonError(ModelGatewayError):
    def __init__(
        self, message: str = "The model response did not contain one JSON object."
    ) -> None:
        super().__init__(ModelErrorCode.MODEL_INVALID_JSON, message, retryable=False)


class ModelSchemaInvalidError(ModelGatewayError):
    def __init__(
        self, message: str = "The model response failed schema validation."
    ) -> None:
        super().__init__(ModelErrorCode.MODEL_SCHEMA_INVALID, message, retryable=False)


class ModelBusinessValidationError(ModelGatewayError):
    def __init__(
        self, message: str = "The model response failed business validation."
    ) -> None:
        super().__init__(
            ModelErrorCode.MODEL_BUSINESS_VALIDATION_FAILED,
            message,
            retryable=False,
        )


class ModelProviderUnavailableError(ModelGatewayError):
    def __init__(
        self, message: str = "The configured model provider is unavailable."
    ) -> None:
        super().__init__(
            ModelErrorCode.MODEL_PROVIDER_UNAVAILABLE,
            message,
            retryable=False,
        )


def error_for_http_status(status_code: int) -> ModelGatewayError:
    """Map the supported OpenAI-compatible HTTP status contract."""

    if status_code == 401:
        return ModelAuthenticationError()
    if status_code == 403:
        return ModelPermissionError()
    if status_code == 429:
        return ModelRateLimitedError()
    if status_code in {500, 502, 503, 504}:
        return ModelServerError()
    return ModelInvalidRequestError()
