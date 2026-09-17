"""FitWeek FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.alerting.runtime import build_alerting_runtime
from app.api.dependencies import (
    build_memory_container,
    build_mysql_calendar_operation_services,
    build_mysql_ics_export_services,
    build_mysql_memory_services,
    build_mysql_profile_agent_service,
    build_mysql_recovery_application_services,
    build_mysql_recovery_draft_services,
    build_mysql_schedule_services,
    build_mysql_session_design_services,
)
from app.api.errors import register_error_handlers
from app.api.router import router
from app.application.checkins import CheckInService
from app.application.exercises import ExerciseCatalogService
from app.application.local_replanning import LocalReplanningService
from app.application.local_user import ensure_local_user
from app.application.plan_generation import PlanGenerationService
from app.application.plans import PlanService
from app.application.profiles import ProfileService
from app.application.progress import ProgressService
from app.application.sessions import SessionService
from app.config import PersistenceBackend, get_settings
from app.domain.exercises.catalog_seed import CATALOG_SEED
from app.infrastructure.redis_client import (
    RedisManager,
    RedisUnavailableError,
    get_redis_manager,
)
from app.infrastructure.redis_memory_cache import RedisMemoryCache
from app.observability.api import router as observability_router
from app.observability.facade import build_observability
from app.observability.http import ObservabilityMiddleware
from app.observability.logging import configure_logging
from app.observability.safety import ObservedSafetyEngine
from app.orchestration.clock import SystemClock
from app.orchestration.mysql_runtime import build_mysql_orchestration_runtime
from app.persistence.database import Database, get_database
from app.persistence.mysql.checkin_repository import MySQLCheckInRepository
from app.persistence.mysql.exercise_repository import MySQLExerciseRepository
from app.persistence.mysql.plan_repository import MySQLPlanRepository
from app.persistence.mysql.profile_draft_repository import MySQLProfileDraftRepository
from app.persistence.mysql.profile_repository import MySQLProfileRepository
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from app.profile_application.apply import ProfileDraftApplyService
from app.profile_application.merge_policy import ProfileDraftMergePolicy
from app.profile_application.preview import ProfileDraftPreviewService
from app.safety.engine import SafetyEngine

logger = logging.getLogger(__name__)


async def _release_calendar_operation_state(application: FastAPI) -> None:
    """Detach Calendar review state before closing its process-owned gateway."""

    gateway = getattr(application.state, "calendar_operation_gateway", None)
    application.state.calendar_operation_repository = None
    application.state.calendar_operation_gateway = None
    application.state.calendar_operation_service = None
    application.state.calendar_operation_run_service = None
    if gateway is not None:
        await gateway.close()


async def _release_recovery_draft_state(application: FastAPI) -> None:
    """Detach Recovery state before closing its process-owned Model Gateway."""

    model_gateway = getattr(application.state, "recovery_draft_model_gateway", None)
    application.state.recovery_draft_repository = None
    application.state.recovery_draft_service = None
    application.state.recovery_draft_model_gateway = None
    application.state.recovery_draft_tool_gateway = None
    application.state.recovery_metrics = None
    if model_gateway is not None:
        await model_gateway.close()


def _release_recovery_application_state(application: FastAPI) -> None:
    """Detach durable Recovery Application services owned by the lifespan."""

    application.state.recovery_application_repository = None
    application.state.recovery_application_service = None
    application.state.recovery_application_run_service = None


@asynccontextmanager
async def _lifespan_impl(application: FastAPI) -> AsyncIterator[None]:
    """Initialize process resources and release the engine on shutdown."""

    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        environment=settings.app_env,
        structured_enabled=(
            settings.observability_enabled and settings.structured_logging_enabled
        ),
    )
    observability = build_observability(
        enabled=settings.observability_enabled,
        tracing_enabled=settings.otel_tracing_enabled,
        exporter_name=settings.otel_exporter.value,
        service_name=settings.otel_service_name,
        metrics_enabled=settings.prometheus_metrics_enabled,
        structured_logging_enabled=settings.structured_logging_enabled,
    )
    database: Database | None = None
    redis_manager: RedisManager | None = None
    application.state.database = None
    application.state.business_container = None
    application.state.local_user = None
    application.state.profile_service = None
    application.state.exercise_service = None
    application.state.plan_service = None
    application.state.plan_generation_service = None
    application.state.profile_agent_service = None
    application.state.profile_agent_model_gateway = None
    application.state.profile_agent_run_service = None
    application.state.profile_draft_preview_service = None
    application.state.profile_draft_apply_service = None
    application.state.profile_draft_repository = None
    application.state.session_design_service = None
    application.state.session_design_plan_application_service = None
    application.state.session_design_repository = None
    application.state.session_design_application_repository = None
    application.state.session_design_model_gateway = None
    application.state.session_design_application_run_service = None
    application.state.schedule_draft_service = None
    application.state.schedule_plan_application_service = None
    application.state.schedule_draft_repository = None
    application.state.schedule_application_repository = None
    application.state.schedule_application_run_service = None
    application.state.schedule_model_gateway = None
    application.state.schedule_calendar_gateway = None
    application.state.session_service = None
    application.state.local_replanning_service = None
    application.state.check_in_service = None
    application.state.progress_service = None
    application.state.memory_application_service = None
    application.state.context_application_service = None
    application.state.memory_repository = None
    application.state.context_snapshot_repository = None
    application.state.ics_export_repository = None
    application.state.ics_export_service = None
    application.state.ics_tool_gateway = None
    application.state.calendar_operation_repository = None
    application.state.calendar_operation_gateway = None
    application.state.calendar_operation_service = None
    application.state.recovery_draft_repository = None
    application.state.recovery_draft_service = None
    application.state.recovery_draft_model_gateway = None
    application.state.recovery_draft_tool_gateway = None
    application.state.recovery_metrics = None
    application.state.recovery_application_repository = None
    application.state.recovery_application_service = None
    application.state.recovery_application_run_service = None
    application.state.observability = observability
    alerting = build_alerting_runtime(
        enabled=settings.alerting_enabled,
        sink_name=settings.alert_notification_sink,
        observability=observability,
    )
    application.state.alerting = alerting

    if settings.persistence_backend is PersistenceBackend.MEMORY:
        container = build_memory_container(settings, observability=observability)
        application.state.business_container = container
        application.state.local_user = container.development_user
        if container.orchestrator_pool is not None:
            try:
                await container.orchestrator_pool.start()
            except Exception as exc:
                container.orchestrator_startup_failed = True
                logger.error(
                    "orchestrator_startup_failed",
                    extra={"error_type": type(exc).__name__},
                )
    else:
        database = get_database()
        if isinstance(database, Database):
            application.state.database = database
            await database.check_connection()
            application.state.local_user = await ensure_local_user(
                MySQLUserAccountRepository(database.session_factory), settings
            )
            application.state.profile_service = ProfileService(
                MySQLProfileRepository(database.session_factory)
            )
            exercises = MySQLExerciseRepository(database.session_factory)
            await exercises.seed(CATALOG_SEED)
            plans = MySQLPlanRepository(database.session_factory)
            ics_services = build_mysql_ics_export_services(
                settings,
                plans=plans,
                exercises=exercises,
                sessions=database.session_factory,
                observability=observability,
            )
            application.state.ics_export_repository = ics_services.repository
            application.state.ics_tool_gateway = ics_services.gateway
            application.state.ics_export_service = ics_services.service
            calendar_operations = build_mysql_calendar_operation_services(
                settings,
                plans=plans,
                sessions=database.session_factory,
                clock=SystemClock(),
            )
            application.state.calendar_operation_repository = (
                calendar_operations.repository
            )
            application.state.calendar_operation_gateway = calendar_operations.gateway
            application.state.calendar_operation_service = calendar_operations.service
            checkins = MySQLCheckInRepository(database.session_factory)
            safety = ObservedSafetyEngine(SafetyEngine(), observability)
            application.state.exercise_service = ExerciseCatalogService(exercises)
            application.state.plan_service = PlanService(
                profiles=MySQLProfileRepository(database.session_factory),
                exercises=exercises,
                plans=plans,
                safety_engine=safety,
            )
            application.state.session_service = SessionService(plans)
            application.state.local_replanning_service = LocalReplanningService(
                profiles=MySQLProfileRepository(database.session_factory),
                exercises=exercises,
                plans=plans,
                check_ins=checkins,
                safety_engine=safety,
            )
            application.state.check_in_service = CheckInService(
                plans=plans, check_ins=checkins
            )
            application.state.progress_service = ProgressService(
                plans=plans, check_ins=checkins
            )
            try:
                (
                    application.state.memory_application_service,
                    application.state.context_application_service,
                    application.state.memory_repository,
                    application.state.context_snapshot_repository,
                ) = build_mysql_memory_services(
                    settings,
                    profiles=MySQLProfileRepository(database.session_factory),
                    sessions=database.session_factory,
                    observability=observability,
                    cache=(
                        RedisMemoryCache(
                            get_redis_manager(), settings.memory_cache_ttl_seconds
                        )
                        if settings.redis_enabled
                        else None
                    ),
                )
            except BaseException:
                try:
                    await _release_calendar_operation_state(application)
                except Exception:
                    pass
                raise
            profile_drafts = MySQLProfileDraftRepository(database.session_factory)
            application.state.profile_draft_repository = profile_drafts
            (
                application.state.profile_agent_service,
                application.state.profile_agent_model_gateway,
            ) = build_mysql_profile_agent_service(
                settings,
                profiles=MySQLProfileRepository(database.session_factory),
                contexts=application.state.context_application_service,
                drafts=profile_drafts,
                observability=observability,
            )
            application.state.profile_draft_preview_service = (
                ProfileDraftPreviewService(
                    reviews=profile_drafts,
                    profiles=MySQLProfileRepository(database.session_factory),
                    merge_policy=ProfileDraftMergePolicy(),
                )
            )
            application.state.profile_draft_apply_service = ProfileDraftApplyService(
                reviews=profile_drafts,
                previews=application.state.profile_draft_preview_service,
            )
            application.state.plan_generation_service = PlanGenerationService(
                profiles=MySQLProfileRepository(database.session_factory),
                exercises=exercises,
                plans=plans,
                safety_engine=safety,
                contexts=application.state.context_application_service,
            )
            (
                application.state.session_design_service,
                application.state.session_design_plan_application_service,
                application.state.session_design_repository,
                application.state.session_design_application_repository,
                application.state.session_design_model_gateway,
            ) = build_mysql_session_design_services(
                settings,
                profiles=MySQLProfileRepository(database.session_factory),
                exercises=exercises,
                plans=plans,
                check_ins=checkins,
                contexts=application.state.context_application_service,
                context_snapshots=application.state.context_snapshot_repository,
                sessions=database.session_factory,
                safety=safety,
                observability=observability,
            )
            (
                application.state.schedule_draft_service,
                application.state.schedule_plan_application_service,
                application.state.schedule_draft_repository,
                application.state.schedule_application_repository,
                application.state.schedule_model_gateway,
                application.state.schedule_calendar_gateway,
            ) = build_mysql_schedule_services(
                settings,
                user=application.state.local_user,
                profiles=MySQLProfileRepository(database.session_factory),
                exercises=exercises,
                plans=plans,
                check_ins=checkins,
                contexts=application.state.context_application_service,
                context_snapshots=(application.state.context_snapshot_repository),
                sessions=database.session_factory,
                safety=safety,
                observability=observability,
            )
            recovery_drafts = build_mysql_recovery_draft_services(
                settings,
                profiles=MySQLProfileRepository(database.session_factory),
                exercises=exercises,
                plans=plans,
                check_ins=checkins,
                calendar_operations=calendar_operations.repository,
                contexts=application.state.context_application_service,
                sessions=database.session_factory,
                observability=observability,
            )
            application.state.recovery_draft_repository = recovery_drafts.repository
            application.state.recovery_draft_service = recovery_drafts.service
            application.state.recovery_draft_model_gateway = (
                recovery_drafts.model_gateway
            )
            application.state.recovery_draft_tool_gateway = recovery_drafts.tool_gateway
            application.state.recovery_metrics = recovery_drafts.metrics
            recovery_applications = build_mysql_recovery_application_services(
                recovery_drafts=recovery_drafts.repository,
                profiles=MySQLProfileRepository(database.session_factory),
                exercises=exercises,
                plans=plans,
                check_ins=checkins,
                session_designs=application.state.session_design_repository,
                schedules=application.state.schedule_draft_repository,
                session_design_service=application.state.session_design_service,
                schedule_service=application.state.schedule_draft_service,
                memories=application.state.memory_application_service,
                calendar_operations=application.state.calendar_operation_service,
                sessions=database.session_factory,
                safety=safety,
                tool_gateway=recovery_drafts.tool_gateway,
            )
            application.state.recovery_application_repository = (
                recovery_applications.repository
            )
            application.state.recovery_application_service = (
                recovery_applications.service
            )
            application.state.mysql_orchestration_runtime = build_mysql_orchestration_runtime(  # noqa: E501
                settings=settings,
                sessions=database.session_factory,
                user=application.state.local_user,
                profiles=MySQLProfileRepository(database.session_factory),
                exercises=exercises,
                plans=plans,
                plan_service=application.state.plan_service,
                plan_generation_service=application.state.plan_generation_service,
                contexts=application.state.context_application_service,
                safety_engine=safety,
                profile_agent_service=application.state.profile_agent_service,
                profile_draft_reviews=profile_drafts,
                profile_draft_apply_service=(
                    application.state.profile_draft_apply_service
                ),
                profile_agent_model_gateway=(
                    application.state.profile_agent_model_gateway
                ),
                session_design_drafts=application.state.session_design_repository,
                session_design_context_snapshots=(
                    application.state.context_snapshot_repository
                ),
                session_design_plan_application_service=(
                    application.state.session_design_plan_application_service
                ),
                local_replanning_service=application.state.local_replanning_service,
                session_design_model_gateway=(
                    application.state.session_design_model_gateway
                ),
                schedule_drafts=application.state.schedule_draft_repository,
                schedule_plan_application_service=(
                    application.state.schedule_plan_application_service
                ),
                schedule_model_gateway=application.state.schedule_model_gateway,
                schedule_calendar_gateway=(application.state.schedule_calendar_gateway),
                calendar_operation_service=application.state.calendar_operation_service,
                recovery_drafts=application.state.recovery_draft_repository,
                recovery_application_service=(
                    application.state.recovery_application_service
                ),
            )
            mysql_runtime = application.state.mysql_orchestration_runtime
            application.state.orchestration_repository = mysql_runtime.repository
            application.state.orchestration_service = mysql_runtime.service
            application.state.profile_agent_run_service = (
                mysql_runtime.profile_agent_run_service
            )
            application.state.session_design_application_run_service = (
                mysql_runtime.session_design_application_run_service
            )
            application.state.schedule_application_run_service = (
                mysql_runtime.schedule_application_run_service
            )
            application.state.calendar_operation_run_service = (
                mysql_runtime.calendar_operation_run_service
            )
            application.state.recovery_application_run_service = (
                mysql_runtime.recovery_application_run_service
            )
        if settings.redis_enabled:
            redis_manager = get_redis_manager()
            try:
                await redis_manager.start()
            except RedisUnavailableError as exc:
                logger.warning(
                    "redis_startup_degraded",
                    extra={"error_type": type(exc).__name__},
                )
    logger.info(
        "application_started",
        extra={"persistence_backend": settings.persistence_backend.value},
    )
    try:
        yield
    finally:
        _release_recovery_application_state(application)
        await _release_recovery_draft_state(application)
        await _release_calendar_operation_state(application)
        cleanup_container = getattr(application.state, "business_container", None)
        if (
            cleanup_container is not None
            and cleanup_container.orchestrator_pool is not None
        ):
            await cleanup_container.orchestrator_pool.stop()
        if cleanup_container is not None:
            await cleanup_container.calendar_read_gateway.close()
            await cleanup_container.calendar_write_gateway.close()
            await cleanup_container.model_gateway.close()
        profile_agent_gateway = getattr(
            application.state, "profile_agent_model_gateway", None
        )
        if profile_agent_gateway is not None:
            await profile_agent_gateway.close()
        session_design_gateway = getattr(
            application.state, "session_design_model_gateway", None
        )
        if session_design_gateway is not None:
            await session_design_gateway.close()
        schedule_gateway = getattr(application.state, "schedule_model_gateway", None)
        if schedule_gateway is not None:
            await schedule_gateway.close()
        schedule_calendar = getattr(
            application.state, "schedule_calendar_gateway", None
        )
        if schedule_calendar is not None:
            await schedule_calendar.close()
        application.state.business_container = None
        application.state.local_user = None
        application.state.profile_service = None
        application.state.exercise_service = None
        application.state.plan_service = None
        application.state.plan_generation_service = None
        application.state.profile_agent_service = None
        application.state.profile_agent_model_gateway = None
        application.state.profile_agent_run_service = None
        application.state.profile_draft_preview_service = None
        application.state.profile_draft_apply_service = None
        application.state.profile_draft_repository = None
        application.state.session_design_service = None
        application.state.session_design_plan_application_service = None
        application.state.session_design_repository = None
        application.state.session_design_application_repository = None
        application.state.session_design_model_gateway = None
        application.state.session_design_application_run_service = None
        application.state.schedule_draft_service = None
        application.state.schedule_plan_application_service = None
        application.state.schedule_draft_repository = None
        application.state.schedule_application_repository = None
        application.state.schedule_application_run_service = None
        application.state.calendar_operation_run_service = None
        application.state.schedule_model_gateway = None
        application.state.schedule_calendar_gateway = None
        application.state.session_service = None
        application.state.local_replanning_service = None
        application.state.check_in_service = None
        application.state.progress_service = None
        application.state.memory_application_service = None
        application.state.context_application_service = None
        application.state.memory_repository = None
        application.state.context_snapshot_repository = None
        application.state.ics_export_repository = None
        application.state.ics_export_service = None
        application.state.ics_tool_gateway = None
        application.state.database = None
        application.state.alerting = None
        observability.shutdown()
        if redis_manager is not None:
            await redis_manager.close()
        if database is not None:
            await database.dispose()
        logger.info("application_stopped")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Guard typed owned state across startup and shutdown failures."""

    try:
        async with _lifespan_impl(application):
            yield
    except BaseException:
        try:
            _release_recovery_application_state(application)
        except Exception:
            # Never replace the original startup or shutdown failure.
            pass
        try:
            await _release_recovery_draft_state(application)
        except Exception:
            # Never replace the original startup or shutdown failure.
            pass
        try:
            await _release_calendar_operation_state(application)
        except Exception:
            # Never replace the original startup or shutdown failure.
            pass
        raise
    else:
        _release_recovery_application_state(application)
        await _release_recovery_draft_state(application)
        await _release_calendar_operation_state(application)


def create_application() -> FastAPI:
    """Build the FastAPI application without connecting to external systems."""

    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.8.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "If-Match", "X-Client-Request-Id"],
    )
    application.add_middleware(ObservabilityMiddleware)
    application.include_router(router)
    application.include_router(observability_router)
    register_error_handlers(application)
    return application


app = create_application()
