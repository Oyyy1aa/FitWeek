"""Durable MySQL composition for the deterministic planning workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.calendar_operation_orchestration import CalendarOperationRunService
from app.application.calendar_operations import CalendarOperationService
from app.application.contexts import ContextApplicationService
from app.application.local_replanning import LocalReplanningService
from app.application.local_user import ensure_local_user
from app.application.orchestration import OrchestrationService
from app.application.plan_generation import PlanGenerationService
from app.application.plans import PlanService
from app.application.profile_agent import ProfileAgentService
from app.application.profile_agent_orchestration import ProfileAgentRunService
from app.application.recovery_application_orchestration import (
    RecoveryApplicationRunService,
)
from app.application.recovery_applications import RecoveryApplicationService
from app.application.schedule_application import SchedulePlanApplicationService
from app.application.schedule_application_orchestration import (
    ScheduleApplicationRunService,
)
from app.application.session_design_application import (
    SessionDesignPlanApplicationService,
)
from app.application.session_design_orchestration import (
    SessionDesignApplicationRunService,
)
from app.calendar_operations.gateway import CalendarWriteGateway
from app.calendar_read.gateway import CalendarReadGateway
from app.config import Settings
from app.domain.context.repositories import ContextSnapshotRepository
from app.domain.exercises.catalog_seed import CATALOG_SEED
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.plans.repositories import PlanRepository
from app.domain.profile_agent.repositories import ProfileDraftReviewRepository
from app.domain.profiles.repositories import ProfileRepository
from app.domain.recovery.repositories import RecoveryDraftRepository
from app.domain.scheduling.repositories import ScheduleDraftRepository
from app.domain.session_design.repositories import SessionDesignRepository
from app.domain.users.models import UserAccount
from app.model_gateway.gateway import ModelGateway
from app.orchestration.calendar_operation_workflow_handlers import (
    ExecuteCalendarOperationItemsHandler,
    FinalizeCalendarOperationHandler,
    LoadCalendarOperationDraftHandler,
    ValidateCalendarOperationApprovalHandler,
    VerifyCalendarOperationResultsHandler,
)
from app.orchestration.clock import SystemClock
from app.orchestration.handler import StepHandler
from app.orchestration.handler_registry import HandlerRegistry
from app.orchestration.metrics import OrchestratorMetrics
from app.orchestration.profile_workflow_handlers import (
    ApplyProfileDraftHandler,
    FinalizeProfileRunHandler,
    ParseProfileRequestHandler,
    WaitForProfileDraftReviewHandler,
)
from app.orchestration.reaper import StepReaper
from app.orchestration.recovery_application_workflow_handlers import (
    BuildRecoveryPlanRevisionHandler,
    CreateRecoverySubdraftsHandler,
    FinalizeRecoveryApplicationHandler,
    LoadRecoveryApplicationContextHandler,
    ResolveRecoveryActionsHandler,
    ValidateRecoveryDraftHandler,
    VerifyRecoveryPlanSafetyHandler,
    WaitForRecoveryRevisionConfirmationHandler,
    WaitForRecoverySubdraftReviewsHandler,
)
from app.orchestration.retry_policy import RetryPolicy
from app.orchestration.schedule_application_workflow_handlers import (
    BuildSchedulePlanRevisionHandler,
    FinalizeScheduleApplicationHandler,
    LoadScheduleApplicationContextHandler,
    RevalidateCalendarBusyHandler,
    ValidateScheduleApplicationHandler,
    VerifySchedulePlanSafetyHandler,
    WaitForScheduleRevisionConfirmationHandler,
)
from app.orchestration.session_design_workflow_handlers import (
    BuildSessionPlanRevisionHandler,
    FinalizeSessionApplicationHandler,
    LoadSessionApplicationContextHandler,
    ValidateSessionDesignTargetHandler,
    VerifySessionPlanSafetyHandler,
    WaitForPlanRevisionConfirmationHandler,
)
from app.orchestration.worker import OrchestrationWorker
from app.orchestration.workflow_handlers import (
    FinalizeRunHandler,
    GenerateDeterministicPlanHandler,
    LoadProfileContextHandler,
    VerifyPlanSafetyHandler,
    WaitForUserConfirmationHandler,
)
from app.persistence.database import Database
from app.persistence.mysql.exercise_repository import MySQLExerciseRepository
from app.persistence.mysql.orchestration_repository import (
    MySQLOrchestrationRepository,
)
from app.persistence.mysql.plan_repository import MySQLPlanRepository
from app.persistence.mysql.profile_repository import MySQLProfileRepository
from app.persistence.mysql.user_repository import MySQLUserAccountRepository
from app.profile_application.apply import ProfileDraftApplyService
from app.safety.engine import SafetyEngine


@dataclass(frozen=True, slots=True)
class MySQLOrchestrationRuntime:
    repository: MySQLOrchestrationRepository
    service: OrchestrationService
    registry: HandlerRegistry
    worker: OrchestrationWorker
    reaper: StepReaper
    profile_agent_run_service: ProfileAgentRunService | None = None
    profile_agent_model_gateway: ModelGateway | None = None
    session_design_application_run_service: (
        SessionDesignApplicationRunService | None
    ) = None
    session_design_model_gateway: ModelGateway | None = None
    schedule_application_run_service: ScheduleApplicationRunService | None = None
    schedule_model_gateway: ModelGateway | None = None
    schedule_calendar_gateway: CalendarReadGateway | None = None
    calendar_operation_service: CalendarOperationService | None = None
    calendar_operation_run_service: CalendarOperationRunService | None = None
    calendar_operation_gateway: CalendarWriteGateway | None = None
    recovery_application_service: RecoveryApplicationService | None = None
    recovery_application_run_service: RecoveryApplicationRunService | None = None
    recovery_draft_model_gateway: ModelGateway | None = None


def build_mysql_orchestration_runtime(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    user: UserAccount,
    profiles: ProfileRepository,
    exercises: ExerciseRepository,
    plans: PlanRepository,
    plan_service: PlanService,
    plan_generation_service: PlanGenerationService,
    contexts: ContextApplicationService,
    safety_engine: SafetyEngine,
    profile_agent_service: ProfileAgentService | None = None,
    profile_draft_reviews: ProfileDraftReviewRepository | None = None,
    profile_draft_apply_service: ProfileDraftApplyService | None = None,
    profile_agent_model_gateway: ModelGateway | None = None,
    session_design_drafts: SessionDesignRepository | None = None,
    session_design_context_snapshots: ContextSnapshotRepository | None = None,
    session_design_plan_application_service: (
        SessionDesignPlanApplicationService | None
    ) = None,
    local_replanning_service: LocalReplanningService | None = None,
    session_design_model_gateway: ModelGateway | None = None,
    schedule_drafts: ScheduleDraftRepository | None = None,
    schedule_plan_application_service: SchedulePlanApplicationService | None = None,
    schedule_model_gateway: ModelGateway | None = None,
    schedule_calendar_gateway: CalendarReadGateway | None = None,
    calendar_operation_service: CalendarOperationService | None = None,
    calendar_operation_gateway: CalendarWriteGateway | None = None,
    recovery_drafts: RecoveryDraftRepository | None = None,
    recovery_application_service: RecoveryApplicationService | None = None,
    recovery_draft_model_gateway: ModelGateway | None = None,
    worker_id: str = "mysql-runtime",
) -> MySQLOrchestrationRuntime:
    """Build one process-local executor over durable MySQL facts.

    The API consumes only ``service`` and never starts ``worker`` or ``reaper``.
    A CLI process consumes the same composition with its own engine/session factory.
    """

    repository = MySQLOrchestrationRepository(
        sessions,
        execution_user_id=user.id,
    )
    clock = SystemClock()
    retry_policy = RetryPolicy()
    metrics = OrchestratorMetrics()
    service = OrchestrationService(
        repository=repository,
        plan_service=plan_service,
        clock=clock,
        retry_policy=retry_policy,
        metrics=metrics,
        enabled=settings.orchestrator_enabled,
    )
    registry = HandlerRegistry()
    handlers: list[StepHandler] = [
        LoadProfileContextHandler(
            profiles=profiles,
            exercises=exercises,
            contexts=contexts,
            user=user,
        ),
        GenerateDeterministicPlanHandler(service=plan_generation_service, user=user),
        VerifyPlanSafetyHandler(
            profiles=profiles,
            exercises=exercises,
            plans=plans,
            safety_engine=safety_engine,
        ),
        WaitForUserConfirmationHandler(),
        FinalizeRunHandler(plans=plans),
    ]
    profile_run_service = None
    session_design_run_service = None
    schedule_run_service = None
    calendar_run_service = None
    recovery_run_service = None
    if calendar_operation_service is not None:
        handlers.extend(
            (
                LoadCalendarOperationDraftHandler(calendar_operation_service, user),
                ValidateCalendarOperationApprovalHandler(
                    calendar_operation_service, user
                ),
                ExecuteCalendarOperationItemsHandler(calendar_operation_service, user),
                VerifyCalendarOperationResultsHandler(calendar_operation_service, user),
                FinalizeCalendarOperationHandler(calendar_operation_service, user),
            )
        )
        calendar_run_service = CalendarOperationRunService(
            repository=repository,
            operations=calendar_operation_service,
            clock=clock,
            retry_policy=retry_policy,
            enabled=settings.orchestrator_enabled,
        )
    if (
        profile_agent_service is not None
        and profile_draft_reviews is not None
        and profile_draft_apply_service is not None
    ):
        handlers.extend(
            (
                ParseProfileRequestHandler(service=profile_agent_service, user=user),
                WaitForProfileDraftReviewHandler(),
                ApplyProfileDraftHandler(
                    service=profile_draft_apply_service,
                    user=user,
                ),
                FinalizeProfileRunHandler(
                    reviews=profile_draft_reviews,
                    profiles=profiles,
                    apply_service=profile_draft_apply_service,
                    user=user,
                ),
            )
        )
        profile_run_service = ProfileAgentRunService(
            repository=repository,
            apply_service=profile_draft_apply_service,
            clock=clock,
            retry_policy=retry_policy,
            enabled=settings.orchestrator_enabled,
        )
    if (
        session_design_drafts is not None
        and session_design_context_snapshots is not None
        and session_design_plan_application_service is not None
        and local_replanning_service is not None
    ):
        handlers.extend(
            (
                LoadSessionApplicationContextHandler(
                    drafts=session_design_drafts,
                    contexts=session_design_context_snapshots,
                    plans=plans,
                    user=user,
                ),
                ValidateSessionDesignTargetHandler(
                    service=session_design_plan_application_service,
                    user=user,
                ),
                BuildSessionPlanRevisionHandler(
                    service=session_design_plan_application_service,
                    user=user,
                ),
                VerifySessionPlanSafetyHandler(
                    service=session_design_plan_application_service,
                    user=user,
                ),
                WaitForPlanRevisionConfirmationHandler(),
                FinalizeSessionApplicationHandler(
                    service=session_design_plan_application_service,
                    plans=plans,
                    user=user,
                ),
            )
        )
        session_design_run_service = SessionDesignApplicationRunService(
            repository=repository,
            drafts=session_design_drafts,
            revisions=local_replanning_service,
            clock=clock,
            retry_policy=retry_policy,
            enabled=settings.orchestrator_enabled,
        )
    if (
        schedule_drafts is not None
        and schedule_plan_application_service is not None
        and local_replanning_service is not None
        and schedule_model_gateway is not None
        and schedule_calendar_gateway is not None
    ):
        handlers.extend(
            (
                LoadScheduleApplicationContextHandler(schedule_drafts, user),
                ValidateScheduleApplicationHandler(
                    schedule_plan_application_service, user
                ),
                RevalidateCalendarBusyHandler(schedule_plan_application_service, user),
                BuildSchedulePlanRevisionHandler(
                    schedule_plan_application_service, user
                ),
                VerifySchedulePlanSafetyHandler(
                    schedule_plan_application_service, user
                ),
                WaitForScheduleRevisionConfirmationHandler(),
                FinalizeScheduleApplicationHandler(
                    schedule_plan_application_service, plans, user
                ),
            )
        )
        schedule_run_service = ScheduleApplicationRunService(
            repository=repository,
            drafts=schedule_drafts,
            revisions=local_replanning_service,
            clock=clock,
            retry_policy=retry_policy,
            enabled=settings.orchestrator_enabled,
        )
    if (
        recovery_drafts is not None
        and recovery_application_service is not None
        and session_design_drafts is not None
        and schedule_drafts is not None
        and local_replanning_service is not None
    ):
        handlers.extend(
            (
                LoadRecoveryApplicationContextHandler(
                    recovery_application_service, user
                ),
                ValidateRecoveryDraftHandler(recovery_application_service, user),
                ResolveRecoveryActionsHandler(recovery_application_service, user),
                CreateRecoverySubdraftsHandler(recovery_application_service, user),
                WaitForRecoverySubdraftReviewsHandler(
                    session_designs=session_design_drafts,
                    schedules=schedule_drafts,
                    user=user,
                ),
                BuildRecoveryPlanRevisionHandler(recovery_application_service, user),
                VerifyRecoveryPlanSafetyHandler(recovery_application_service, user),
                WaitForRecoveryRevisionConfirmationHandler(),
                FinalizeRecoveryApplicationHandler(
                    service=recovery_application_service,
                    plans=plans,
                    user=user,
                ),
            )
        )
        recovery_run_service = RecoveryApplicationRunService(
            repository=repository,
            drafts=recovery_drafts,
            session_designs=session_design_drafts,
            schedules=schedule_drafts,
            revisions=local_replanning_service,
            clock=clock,
            retry_policy=retry_policy,
            enabled=settings.orchestrator_enabled,
        )
    for handler in handlers:
        registry.register(handler)
    worker = OrchestrationWorker(
        worker_id=worker_id,
        repository=repository,
        registry=registry,
        clock=clock,
        retry_policy=retry_policy,
        lease_duration=timedelta(seconds=settings.orchestrator_lease_seconds),
        handler_timeout_seconds=settings.orchestrator_handler_timeout_seconds,
        poll_interval_seconds=settings.orchestrator_poll_interval_seconds,
    )
    return MySQLOrchestrationRuntime(
        repository=repository,
        service=service,
        registry=registry,
        worker=worker,
        reaper=StepReaper(
            repository=repository,
            clock=clock,
            retry_policy=retry_policy,
        ),
        profile_agent_run_service=profile_run_service,
        profile_agent_model_gateway=profile_agent_model_gateway,
        session_design_application_run_service=session_design_run_service,
        session_design_model_gateway=session_design_model_gateway,
        schedule_application_run_service=schedule_run_service,
        schedule_model_gateway=schedule_model_gateway,
        schedule_calendar_gateway=schedule_calendar_gateway,
        calendar_operation_service=calendar_operation_service,
        calendar_operation_run_service=calendar_run_service,
        calendar_operation_gateway=calendar_operation_gateway,
        recovery_application_service=recovery_application_service,
        recovery_application_run_service=recovery_run_service,
        recovery_draft_model_gateway=recovery_draft_model_gateway,
    )


async def build_mysql_cli_runtime(
    settings: Settings, *, worker_id: str
) -> tuple[MySQLOrchestrationRuntime, Database]:
    """Compose an independent CLI process without importing FastAPI or Uvicorn."""

    from app.api.dependencies import (
        build_mysql_calendar_executor_services,
        build_mysql_calendar_operation_services,
        build_mysql_memory_services,
        build_mysql_profile_agent_service,
        build_mysql_recovery_application_services,
        build_mysql_recovery_draft_services,
        build_mysql_schedule_services,
        build_mysql_session_design_services,
    )
    from app.application.local_replanning import LocalReplanningService
    from app.persistence.mysql.checkin_repository import MySQLCheckInRepository
    from app.persistence.mysql.profile_draft_repository import (
        MySQLProfileDraftRepository,
    )
    from app.profile_application.apply import ProfileDraftApplyService
    from app.profile_application.merge_policy import ProfileDraftMergePolicy
    from app.profile_application.preview import ProfileDraftPreviewService

    database = Database(settings)
    profile_agent_gateway: ModelGateway | None = None
    session_design_gateway: ModelGateway | None = None
    schedule_gateway: ModelGateway | None = None
    schedule_calendar: CalendarReadGateway | None = None
    calendar_gateway: CalendarWriteGateway | None = None
    recovery_draft_gateway: ModelGateway | None = None
    try:
        await database.check_connection()
        user = await ensure_local_user(
            MySQLUserAccountRepository(database.session_factory), settings
        )
        profiles = MySQLProfileRepository(database.session_factory)
        exercises = MySQLExerciseRepository(database.session_factory)
        await exercises.seed(CATALOG_SEED)
        plans = MySQLPlanRepository(database.session_factory)
        checkins = MySQLCheckInRepository(database.session_factory)
        plan_service = PlanService(
            profiles=profiles,
            exercises=exercises,
            plans=plans,
            safety_engine=SafetyEngine(),
        )
        memory_applications, contexts, _, context_snapshots = (
            build_mysql_memory_services(
                settings,
                profiles=profiles,
                sessions=database.session_factory,
                cache=None,
            )
        )
        profile_drafts = MySQLProfileDraftRepository(database.session_factory)
        profile_agent, profile_agent_gateway = build_mysql_profile_agent_service(
            settings,
            profiles=profiles,
            contexts=contexts,
            drafts=profile_drafts,
        )
        profile_preview = ProfileDraftPreviewService(
            reviews=profile_drafts,
            profiles=profiles,
            merge_policy=ProfileDraftMergePolicy(),
        )
        profile_apply = ProfileDraftApplyService(
            reviews=profile_drafts,
            previews=profile_preview,
        )
        local_replanning = LocalReplanningService(
            profiles=profiles,
            exercises=exercises,
            plans=plans,
            check_ins=checkins,
            safety_engine=SafetyEngine(),
        )
        (
            session_design_service,
            session_design_application,
            session_design_drafts,
            _session_design_applications,
            session_design_gateway,
        ) = build_mysql_session_design_services(
            settings,
            profiles=profiles,
            exercises=exercises,
            plans=plans,
            check_ins=checkins,
            contexts=contexts,
            context_snapshots=context_snapshots,
            sessions=database.session_factory,
            safety=SafetyEngine(),
        )
        (
            schedule_draft_service,
            schedule_application,
            schedule_drafts,
            _schedule_applications,
            schedule_gateway,
            schedule_calendar,
        ) = build_mysql_schedule_services(
            settings,
            user=user,
            profiles=profiles,
            exercises=exercises,
            plans=plans,
            check_ins=checkins,
            contexts=contexts,
            context_snapshots=context_snapshots,
            sessions=database.session_factory,
            safety=SafetyEngine(),
        )
        generation = PlanGenerationService(
            profiles=profiles,
            exercises=exercises,
            plans=plans,
            safety_engine=SafetyEngine(),
            contexts=contexts,
        )
        calendar_executor = build_mysql_calendar_executor_services(
            settings,
            user=user,
            plans=plans,
            exercises=exercises,
            sessions=database.session_factory,
            clock=SystemClock(),
        )
        calendar_gateway = calendar_executor.gateway
        calendar_operations = build_mysql_calendar_operation_services(
            settings,
            plans=plans,
            sessions=database.session_factory,
            clock=SystemClock(),
        )
        recovery_drafts = build_mysql_recovery_draft_services(
            settings,
            profiles=profiles,
            exercises=exercises,
            plans=plans,
            check_ins=checkins,
            calendar_operations=calendar_operations.repository,
            contexts=contexts,
            sessions=database.session_factory,
        )
        recovery_draft_gateway = recovery_drafts.model_gateway
        recovery_applications = build_mysql_recovery_application_services(
            recovery_drafts=recovery_drafts.repository,
            profiles=profiles,
            exercises=exercises,
            plans=plans,
            check_ins=checkins,
            session_designs=session_design_drafts,
            schedules=schedule_drafts,
            session_design_service=session_design_service,
            schedule_service=schedule_draft_service,
            memories=memory_applications,
            calendar_operations=calendar_operations.service,
            sessions=database.session_factory,
            safety=SafetyEngine(),
            tool_gateway=recovery_drafts.tool_gateway,
        )
        return (
            build_mysql_orchestration_runtime(
                settings=settings,
                sessions=database.session_factory,
                user=user,
                profiles=profiles,
                exercises=exercises,
                plans=plans,
                plan_service=plan_service,
                plan_generation_service=generation,
                contexts=contexts,
                safety_engine=SafetyEngine(),
                profile_agent_service=profile_agent,
                profile_draft_reviews=profile_drafts,
                profile_draft_apply_service=profile_apply,
                profile_agent_model_gateway=profile_agent_gateway,
                session_design_drafts=session_design_drafts,
                session_design_context_snapshots=context_snapshots,
                session_design_plan_application_service=session_design_application,
                local_replanning_service=local_replanning,
                session_design_model_gateway=session_design_gateway,
                schedule_drafts=schedule_drafts,
                schedule_plan_application_service=schedule_application,
                schedule_model_gateway=schedule_gateway,
                schedule_calendar_gateway=schedule_calendar,
                calendar_operation_service=calendar_executor.service,
                calendar_operation_gateway=calendar_gateway,
                recovery_drafts=recovery_drafts.repository,
                recovery_application_service=recovery_applications.service,
                recovery_draft_model_gateway=recovery_draft_gateway,
                worker_id=worker_id,
            ),
            database,
        )
    except Exception:
        if calendar_gateway is not None:
            await calendar_gateway.close()
        if schedule_calendar is not None:
            await schedule_calendar.close()
        if schedule_gateway is not None:
            await schedule_gateway.close()
        if session_design_gateway is not None:
            await session_design_gateway.close()
        if profile_agent_gateway is not None:
            await profile_agent_gateway.close()
        if recovery_draft_gateway is not None:
            await recovery_draft_gateway.close()
        await database.dispose()
        raise
