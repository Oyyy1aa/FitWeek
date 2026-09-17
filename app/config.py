"""Central application configuration."""

from enum import StrEnum
from functools import lru_cache
from typing import Self
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class PersistenceBackend(StrEnum):
    """Supported persistence composition modes."""

    MEMORY = "memory"
    MYSQL = "mysql"


class AppMode(StrEnum):
    """Supported identity modes for the local FitWeek application."""

    SINGLE_USER = "single_user"


class OTelExporter(StrEnum):
    """Locally supported Phase 8B1 trace exporters."""

    IN_MEMORY = "in_memory"
    DISABLED = "disabled"


class Settings(BaseSettings):
    """Environment-backed settings for the FitWeek API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = Field(default="FitWeek", validation_alias="APP_NAME")
    app_env: str = Field(default="development", validation_alias="APP_ENV")
    app_host: str = Field(default="127.0.0.1", validation_alias="APP_HOST")
    app_port: int = Field(default=8000, ge=1, le=65535, validation_alias="APP_PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    app_mode: AppMode = Field(
        default=AppMode.SINGLE_USER,
        validation_alias="APP_MODE",
    )
    single_user_email: str = Field(
        default="demo@fitweek.local",
        validation_alias="SINGLE_USER_EMAIL",
    )
    single_user_display_name: str = Field(
        default="FitWeek 用户",
        validation_alias="SINGLE_USER_DISPLAY_NAME",
    )
    single_user_timezone: str = Field(
        default="Asia/Shanghai",
        validation_alias="SINGLE_USER_TIMEZONE",
    )
    cors_allowed_origins: tuple[str, ...] = Field(
        default=("http://localhost:5173", "http://127.0.0.1:5173"),
        validation_alias="CORS_ALLOWED_ORIGINS",
    )
    observability_enabled: bool = Field(
        default=True, validation_alias="OBSERVABILITY_ENABLED"
    )
    otel_tracing_enabled: bool = Field(
        default=True, validation_alias="OTEL_TRACING_ENABLED"
    )
    otel_exporter: OTelExporter = Field(
        default=OTelExporter.IN_MEMORY, validation_alias="OTEL_EXPORTER"
    )
    otel_service_name: str = Field(
        default="fitweek-api",
        min_length=1,
        max_length=128,
        validation_alias="OTEL_SERVICE_NAME",
    )
    prometheus_metrics_enabled: bool = Field(
        default=True, validation_alias="PROMETHEUS_METRICS_ENABLED"
    )
    structured_logging_enabled: bool = Field(
        default=True, validation_alias="STRUCTURED_LOGGING_ENABLED"
    )
    alerting_enabled: bool = Field(default=True, validation_alias="ALERTING_ENABLED")
    alert_notification_sink: str = Field(
        default="in_memory",
        pattern="^(in_memory|file_test_sink|disabled)$",
        validation_alias="ALERT_NOTIFICATION_SINK",
    )
    persistence_backend: PersistenceBackend = Field(
        default=PersistenceBackend.MYSQL,
        validation_alias="PERSISTENCE_BACKEND",
    )
    database_url: SecretStr = Field(
        default=SecretStr(
            "mysql+asyncmy://fitweek_app@127.0.0.1:3306/fitweek?charset=utf8mb4"
        ),
        validation_alias="DATABASE_URL",
    )
    database_connect_timeout_seconds: float = Field(
        default=3.0,
        gt=0,
        le=60,
        validation_alias="DATABASE_CONNECT_TIMEOUT_SECONDS",
    )
    database_pool_size: int = Field(
        default=5,
        gt=0,
        validation_alias="DATABASE_POOL_SIZE",
    )
    database_max_overflow: int = Field(
        default=10,
        ge=0,
        validation_alias="DATABASE_MAX_OVERFLOW",
    )
    database_pool_recycle_seconds: int = Field(
        default=1800,
        gt=0,
        validation_alias="DATABASE_POOL_RECYCLE_SECONDS",
    )

    redis_enabled: bool = Field(default=False, validation_alias="REDIS_ENABLED")
    redis_url: SecretStr = Field(
        default=SecretStr("redis://127.0.0.1:6379/0"),
        validation_alias="REDIS_URL",
    )
    redis_connect_timeout_seconds: float = Field(
        default=1.0,
        gt=0,
        le=60,
        validation_alias="REDIS_CONNECT_TIMEOUT_SECONDS",
    )
    redis_socket_timeout_seconds: float = Field(
        default=1.0,
        gt=0,
        le=60,
        validation_alias="REDIS_SOCKET_TIMEOUT_SECONDS",
    )
    redis_max_connections: int = Field(
        default=10,
        gt=0,
        le=1000,
        validation_alias="REDIS_MAX_CONNECTIONS",
    )
    redis_healthcheck_interval_seconds: int = Field(
        default=30,
        gt=0,
        validation_alias=AliasChoices(
            "REDIS_HEALTH_CHECK_INTERVAL_SECONDS",
            "REDIS_HEALTHCHECK_INTERVAL_SECONDS",
        ),
    )
    redis_key_prefix: str = Field(
        default="fitweek",
        min_length=1,
        validation_alias="REDIS_KEY_PREFIX",
    )
    memory_cache_ttl_seconds: int = Field(
        default=60,
        gt=0,
        le=3600,
        validation_alias="MEMORY_CACHE_TTL_SECONDS",
    )
    orchestrator_enabled: bool = Field(
        default=False,
        validation_alias="ORCHESTRATOR_ENABLED",
    )
    orchestrator_worker_count: int = Field(
        default=2,
        ge=1,
        le=16,
        validation_alias="ORCHESTRATOR_WORKER_COUNT",
    )
    orchestrator_poll_interval_seconds: float = Field(
        default=0.1,
        gt=0,
        le=60,
        validation_alias="ORCHESTRATOR_POLL_INTERVAL_SECONDS",
    )
    orchestrator_lease_seconds: float = Field(
        default=10.0,
        gt=0,
        le=3600,
        validation_alias="ORCHESTRATOR_LEASE_SECONDS",
    )
    orchestrator_handler_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=3600,
        validation_alias="ORCHESTRATOR_HANDLER_TIMEOUT_SECONDS",
    )
    orchestrator_reaper_interval_seconds: float = Field(
        default=1.0,
        gt=0,
        le=3600,
        validation_alias="ORCHESTRATOR_REAPER_INTERVAL_SECONDS",
    )
    model_gateway_enabled: bool = Field(
        default=True,
        validation_alias="MODEL_GATEWAY_ENABLED",
    )
    model_primary_provider: str = Field(
        default="scripted-fake",
        validation_alias="MODEL_PRIMARY_PROVIDER",
    )
    model_backup_provider: str = Field(
        default="template-fallback",
        validation_alias="MODEL_BACKUP_PROVIDER",
    )
    model_request_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=120,
        validation_alias="MODEL_REQUEST_TIMEOUT_SECONDS",
    )
    model_connect_timeout_seconds: float = Field(
        default=3.0,
        gt=0,
        le=60,
        validation_alias="MODEL_CONNECT_TIMEOUT_SECONDS",
    )
    model_max_attempts: int = Field(
        default=3,
        ge=1,
        le=4,
        validation_alias="MODEL_MAX_ATTEMPTS",
    )
    model_max_concurrency: int = Field(
        default=4,
        ge=1,
        le=128,
        validation_alias="MODEL_MAX_CONCURRENCY",
    )
    model_rate_limit_per_minute: int = Field(
        default=30,
        ge=1,
        le=10000,
        validation_alias="MODEL_RATE_LIMIT_PER_MINUTE",
    )
    model_max_response_bytes: int = Field(
        default=262144,
        ge=1024,
        le=4 * 1024 * 1024,
        validation_alias="MODEL_MAX_RESPONSE_BYTES",
    )
    model_trace_include_user_content: bool = Field(
        default=False,
        validation_alias="MODEL_TRACE_INCLUDE_USER_CONTENT",
    )
    model_primary_base_url: SecretStr | None = Field(
        default=None,
        validation_alias="MODEL_PRIMARY_BASE_URL",
    )
    model_primary_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="MODEL_PRIMARY_API_KEY",
    )
    model_primary_model: str = Field(
        default="scripted-profile-v1",
        min_length=1,
        validation_alias="MODEL_PRIMARY_MODEL",
    )
    model_primary_provider_version: str = Field(
        default="phase-3a-v1",
        min_length=1,
        validation_alias="MODEL_PRIMARY_PROVIDER_VERSION",
    )
    model_backup_base_url: SecretStr | None = Field(
        default=None,
        validation_alias="MODEL_BACKUP_BASE_URL",
    )
    model_backup_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="MODEL_BACKUP_API_KEY",
    )
    model_backup_model: str = Field(
        default="scripted-profile-backup-v1",
        min_length=1,
        validation_alias="MODEL_BACKUP_MODEL",
    )
    model_backup_provider_version: str = Field(
        default="phase-3a-v1",
        min_length=1,
        validation_alias="MODEL_BACKUP_PROVIDER_VERSION",
    )
    context_max_characters: int = Field(
        default=8000,
        ge=128,
        le=100000,
        validation_alias="CONTEXT_MAX_CHARACTERS",
    )
    context_max_memories: int = Field(
        default=10,
        ge=0,
        le=100,
        validation_alias="CONTEXT_MAX_MEMORIES",
    )
    context_max_behavior_items: int = Field(
        default=10,
        ge=0,
        le=100,
        validation_alias="CONTEXT_MAX_BEHAVIOR_ITEMS",
    )
    context_debug_api_enabled: bool = Field(
        default=False,
        validation_alias="CONTEXT_DEBUG_API_ENABLED",
    )
    schedule_agent_enabled: bool = Field(
        default=True, validation_alias="SCHEDULE_AGENT_ENABLED"
    )
    schedule_slot_granularity_minutes: int = Field(
        default=15, ge=5, le=60, validation_alias="SCHEDULE_SLOT_GRANULARITY_MINUTES"
    )
    schedule_min_buffer_minutes: int = Field(
        default=0, ge=0, le=120, validation_alias="SCHEDULE_MIN_BUFFER_MINUTES"
    )
    schedule_max_candidates_per_session: int = Field(
        default=64, ge=1, le=256, validation_alias="SCHEDULE_MAX_CANDIDATES_PER_SESSION"
    )
    schedule_max_total_candidates: int = Field(
        default=512, ge=1, le=2048, validation_alias="SCHEDULE_MAX_TOTAL_CANDIDATES"
    )
    schedule_draft_ttl_minutes: int = Field(
        default=30, ge=1, le=1440, validation_alias="SCHEDULE_DRAFT_TTL_MINUTES"
    )
    behavior_summary_default_window_days: int = Field(
        default=28,
        ge=1,
        le=56,
        validation_alias="BEHAVIOR_SUMMARY_DEFAULT_WINDOW_DAYS",
    )
    behavior_summary_max_window_days: int = Field(
        default=56,
        ge=1,
        le=366,
        validation_alias="BEHAVIOR_SUMMARY_MAX_WINDOW_DAYS",
    )
    behavior_summary_min_signal_occurrences: int = Field(
        default=3,
        ge=2,
        le=100,
        validation_alias="BEHAVIOR_SUMMARY_MIN_SIGNAL_OCCURRENCES",
    )
    behavior_summary_repeat_ratio_threshold: float = Field(
        default=0.60,
        ge=0.5,
        le=1,
        validation_alias="BEHAVIOR_SUMMARY_REPEAT_RATIO_THRESHOLD",
    )
    behavior_summary_min_rpe_samples: int = Field(
        default=2,
        ge=2,
        le=100,
        validation_alias="BEHAVIOR_SUMMARY_MIN_RPE_SAMPLES",
    )
    recovery_draft_ttl_minutes: int = Field(
        default=30,
        ge=1,
        le=1440,
        validation_alias="RECOVERY_DRAFT_TTL_MINUTES",
    )
    recovery_agent_enabled: bool = Field(
        default=True,
        validation_alias="RECOVERY_AGENT_ENABLED",
    )
    schedule_busy_snapshot_max_age_seconds: int = Field(
        default=300,
        ge=1,
        le=86400,
        validation_alias="SCHEDULE_BUSY_SNAPSHOT_MAX_AGE_SECONDS",
    )
    calendar_read_enabled: bool = Field(
        default=False, validation_alias="CALENDAR_READ_ENABLED"
    )
    calendar_read_provider: str = Field(
        default="none", validation_alias="CALENDAR_READ_PROVIDER"
    )
    calendar_read_base_url: SecretStr | None = Field(
        default=None, validation_alias="CALENDAR_READ_BASE_URL"
    )
    calendar_read_api_key: SecretStr | None = Field(
        default=None, validation_alias="CALENDAR_READ_API_KEY"
    )
    calendar_read_timeout_seconds: float = Field(
        default=3, gt=0, le=60, validation_alias="CALENDAR_READ_TIMEOUT_SECONDS"
    )
    calendar_read_max_attempts: int = Field(
        default=2, ge=1, le=2, validation_alias="CALENDAR_READ_MAX_ATTEMPTS"
    )
    calendar_read_max_response_bytes: int = Field(
        default=131072,
        ge=1024,
        le=1048576,
        validation_alias="CALENDAR_READ_MAX_RESPONSE_BYTES",
    )
    calendar_write_enabled: bool = Field(
        default=False, validation_alias="CALENDAR_WRITE_ENABLED"
    )
    calendar_write_provider: str = Field(
        default="none", validation_alias="CALENDAR_WRITE_PROVIDER"
    )
    calendar_write_base_url: SecretStr | None = Field(
        default=None, validation_alias="CALENDAR_WRITE_BASE_URL"
    )
    calendar_write_api_key: SecretStr | None = Field(
        default=None, validation_alias="CALENDAR_WRITE_API_KEY"
    )
    calendar_write_timeout_seconds: float = Field(
        default=3,
        gt=0,
        le=60,
        validation_alias="CALENDAR_WRITE_TIMEOUT_SECONDS",
    )
    calendar_write_max_attempts: int = Field(
        default=3,
        ge=1,
        le=3,
        validation_alias="CALENDAR_WRITE_MAX_ATTEMPTS",
    )
    calendar_write_max_response_bytes: int = Field(
        default=131072,
        ge=1024,
        le=1048576,
        validation_alias="CALENDAR_WRITE_MAX_RESPONSE_BYTES",
    )
    tool_circuit_failure_threshold: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias="TOOL_CIRCUIT_FAILURE_THRESHOLD",
    )
    tool_circuit_failure_window_seconds: int = Field(
        default=60,
        ge=1,
        le=3600,
        validation_alias="TOOL_CIRCUIT_FAILURE_WINDOW_SECONDS",
    )
    tool_circuit_open_duration_seconds: int = Field(
        default=30,
        ge=1,
        le=3600,
        validation_alias="TOOL_CIRCUIT_OPEN_DURATION_SECONDS",
    )
    tool_retry_budget_maximum_attempts: int = Field(
        default=12,
        ge=1,
        le=100,
        validation_alias="TOOL_RETRY_BUDGET_MAXIMUM_ATTEMPTS",
    )
    calendar_read_bulkhead_limit: int = Field(
        default=10,
        ge=1,
        le=128,
        validation_alias="CALENDAR_READ_BULKHEAD_LIMIT",
    )

    @field_validator("database_url")
    @classmethod
    def require_asyncmy_url(cls, value: SecretStr) -> SecretStr:
        """Reject legacy or synchronous database drivers."""

        try:
            driver_name = make_url(value.get_secret_value()).drivername
        except Exception as exc:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from exc
        if driver_name != "mysql+asyncmy":
            raise ValueError("DATABASE_URL must use the mysql+asyncmy driver")
        return value

    @field_validator("redis_url")
    @classmethod
    def require_redis_url(cls, value: SecretStr) -> SecretStr:
        """Allow only Redis connection URL schemes."""

        url = value.get_secret_value()
        if not url.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must use redis:// or rediss://")
        return value

    @field_validator("redis_key_prefix")
    @classmethod
    def require_non_empty_redis_prefix(cls, value: str) -> str:
        """Reject blank Redis namespaces."""

        normalized = value.strip().strip(":")
        if not normalized:
            raise ValueError("REDIS_KEY_PREFIX must not be blank")
        return normalized

    @field_validator("otel_service_name")
    @classmethod
    def require_non_empty_otel_service_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("OTEL_SERVICE_NAME must not be blank")
        return normalized

    @field_validator(
        "single_user_email",
        "single_user_display_name",
        "single_user_timezone",
    )
    @classmethod
    def require_non_blank_single_user_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("single-user configuration values must not be blank")
        return normalized

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def normalize_cors_origins(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            value = tuple(item.strip() for item in value.split(","))
        if not isinstance(value, (tuple, list)):
            raise ValueError("CORS_ALLOWED_ORIGINS must be a comma-separated list")
        origins = tuple(str(item).strip().rstrip("/") for item in value)
        allowed = {"http://localhost:5173", "http://127.0.0.1:5173"}
        if not origins or any(origin not in allowed for origin in origins):
            raise ValueError("CORS_ALLOWED_ORIGINS only permits local Vite origins")
        return origins

    @field_validator("model_primary_provider", "model_backup_provider")
    @classmethod
    def require_supported_model_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"http", "scripted-fake", "template-fallback"}:
            raise ValueError(
                "model provider must be http, scripted-fake, or template-fallback"
            )
        return normalized

    @field_validator(
        "model_primary_model",
        "model_primary_provider_version",
        "model_backup_model",
        "model_backup_provider_version",
    )
    @classmethod
    def require_non_blank_model_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model configuration value must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_model_provider_configuration(self) -> Self:
        if self.model_primary_provider == "template-fallback":
            raise ValueError("template-fallback cannot be the primary provider")
        self._validate_http_provider(
            role="primary",
            provider=self.model_primary_provider,
            base_url=self.model_primary_base_url,
            api_key=self.model_primary_api_key,
        )
        self._validate_http_provider(
            role="backup",
            provider=self.model_backup_provider,
            base_url=self.model_backup_base_url,
            api_key=self.model_backup_api_key,
        )
        provider = self.calendar_read_provider.strip().lower()
        if provider not in {"none", "scripted", "http"}:
            raise ValueError("CALENDAR_READ_PROVIDER must be none, scripted, or http")
        self.calendar_read_provider = provider
        if self.calendar_read_enabled and provider == "http":
            self._validate_calendar_http()
        write_provider = self.calendar_write_provider.strip().lower()
        if write_provider not in {"none", "scripted", "http"}:
            raise ValueError("CALENDAR_WRITE_PROVIDER must be none, scripted, or http")
        self.calendar_write_provider = write_provider
        if self.calendar_write_enabled and write_provider == "http":
            self._validate_calendar_write_http()
        if (
            self.schedule_max_total_candidates
            < self.schedule_max_candidates_per_session
        ):
            raise ValueError(
                "SCHEDULE_MAX_TOTAL_CANDIDATES must not be smaller than the "
                "per-Session limit"
            )
        if (
            self.behavior_summary_default_window_days
            > self.behavior_summary_max_window_days
        ):
            raise ValueError(
                "BEHAVIOR_SUMMARY_DEFAULT_WINDOW_DAYS must not exceed the maximum"
            )
        return self

    def _validate_calendar_http(self) -> None:
        if self.calendar_read_base_url is None:
            raise ValueError("CALENDAR_READ_BASE_URL is required for HTTP")
        parsed = urlsplit(self.calendar_read_base_url.get_secret_value())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("CALENDAR_READ_BASE_URL must be an HTTP URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("calendar provider URL must not embed credentials")
        if (
            self.calendar_read_api_key is None
            or not self.calendar_read_api_key.get_secret_value().strip()
        ):
            raise ValueError("CALENDAR_READ_API_KEY is required for HTTP")

    def _validate_calendar_write_http(self) -> None:
        if self.calendar_write_base_url is None:
            raise ValueError("CALENDAR_WRITE_BASE_URL is required for HTTP")
        parsed = urlsplit(self.calendar_write_base_url.get_secret_value())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("CALENDAR_WRITE_BASE_URL must be an HTTP URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("calendar write provider URL must not embed credentials")
        if (
            self.calendar_write_api_key is None
            or not self.calendar_write_api_key.get_secret_value().strip()
        ):
            raise ValueError("CALENDAR_WRITE_API_KEY is required for HTTP")

    @staticmethod
    def _validate_http_provider(
        *,
        role: str,
        provider: str,
        base_url: SecretStr | None,
        api_key: SecretStr | None,
    ) -> None:
        if provider != "http":
            return
        if base_url is None or not base_url.get_secret_value().strip():
            raise ValueError(f"MODEL_{role.upper()}_BASE_URL is required for HTTP")
        parsed = urlsplit(base_url.get_secret_value())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"MODEL_{role.upper()}_BASE_URL must be an HTTP URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("model provider base URL must not embed credentials")
        if api_key is None or not api_key.get_secret_value().strip():
            raise ValueError(f"MODEL_{role.upper()}_API_KEY is required for HTTP")


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings instance for the current process."""

    return Settings()
