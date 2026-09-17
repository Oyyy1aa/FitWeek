"""Process-local contract metrics; this is not production monitoring."""

from dataclasses import asdict, dataclass

from app.domain.model_gateway.enums import ModelErrorCode, ProviderRole


@dataclass(frozen=True, slots=True)
class ModelGatewayMetricsSnapshot:
    requests_total: int
    provider_attempts_total: int
    primary_successes: int
    backup_successes: int
    template_fallbacks: int
    timeouts: int
    rate_limited: int
    server_errors: int
    authentication_errors: int
    invalid_json: int
    schema_failures: int
    business_validation_failures: int
    scope_guard_blocks: int
    requests_in_flight: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class ModelGatewayMetrics:
    def __init__(self) -> None:
        self.requests_total = 0
        self.provider_attempts_total = 0
        self.primary_successes = 0
        self.backup_successes = 0
        self.template_fallbacks = 0
        self.timeouts = 0
        self.rate_limited = 0
        self.server_errors = 0
        self.authentication_errors = 0
        self.invalid_json = 0
        self.schema_failures = 0
        self.business_validation_failures = 0
        self.scope_guard_blocks = 0
        self.requests_in_flight = 0

    def request_started(self) -> None:
        self.requests_total += 1
        self.requests_in_flight += 1

    def request_finished(self) -> None:
        self.requests_in_flight = max(0, self.requests_in_flight - 1)

    def provider_attempted(self) -> None:
        self.provider_attempts_total += 1

    def provider_succeeded(self, role: ProviderRole) -> None:
        if role is ProviderRole.PRIMARY:
            self.primary_successes += 1
        elif role is ProviderRole.BACKUP:
            self.backup_successes += 1

    def template_used(self) -> None:
        self.template_fallbacks += 1

    def scope_blocked(self) -> None:
        self.scope_guard_blocks += 1

    def error(self, code: ModelErrorCode) -> None:
        mapping = {
            ModelErrorCode.MODEL_TIMEOUT: "timeouts",
            ModelErrorCode.MODEL_RATE_LIMITED: "rate_limited",
            ModelErrorCode.MODEL_SERVER_ERROR: "server_errors",
            ModelErrorCode.MODEL_AUTHENTICATION_ERROR: "authentication_errors",
            ModelErrorCode.MODEL_INVALID_JSON: "invalid_json",
            ModelErrorCode.MODEL_SCHEMA_INVALID: "schema_failures",
            ModelErrorCode.MODEL_BUSINESS_VALIDATION_FAILED: (
                "business_validation_failures"
            ),
        }
        target = mapping.get(code)
        if target is not None:
            setattr(self, target, getattr(self, target) + 1)

    def snapshot(self) -> ModelGatewayMetricsSnapshot:
        fields = ModelGatewayMetricsSnapshot.__dataclass_fields__
        return ModelGatewayMetricsSnapshot(
            **{field: getattr(self, field) for field in fields}
        )
