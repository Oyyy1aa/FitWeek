"""Application-scoped composition and replaceable FastAPI dependencies."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.profile_agent import ProfileAgent
from app.agents.recovery_agent import RecoveryAgent
from app.agents.schedule_agent import ScheduleAgent
from app.agents.session_designer import SessionDesignerAgent
from app.application.calendar_operation_orchestration import (
    CalendarOperationRunService,
)
from app.application.calendar_operations import CalendarOperationService
from app.application.checkins import CheckInService
from app.application.contexts import ContextApplicationService
from app.application.errors import UnsupportedPersistenceBackend
from app.application.exercises import ExerciseCatalogService
from app.application.ics_export import IcsExportService
from app.application.local_replanning import LocalReplanningService
from app.application.local_user import build_local_user
from app.application.memories import MemoryApplicationService
from app.application.orchestration import OrchestrationService
from app.application.plan_generation import PlanGenerationService
from app.application.plans import PlanService
from app.application.profile_agent import ProfileAgentService
from app.application.profile_agent_orchestration import ProfileAgentRunService
from app.application.profiles import ProfileService
from app.application.progress import ProgressService
from app.application.recovery_application_orchestration import (
    RecoveryApplicationRunService,
)
from app.application.recovery_applications import RecoveryApplicationService
from app.application.recovery_drafts import RecoveryDraftService
from app.application.schedule_application import SchedulePlanApplicationService
from app.application.schedule_application_orchestration import (
    ScheduleApplicationRunService,
)
from app.application.schedules import ScheduleDraftService
from app.application.session_design_application import (
    SessionDesignPlanApplicationService,
)
from app.application.session_design_orchestration import (
    SessionDesignApplicationRunService,
)
from app.application.session_designs import SessionDesignService
from app.application.sessions import SessionService
from app.behavior.metrics import RecoveryMetrics
from app.behavior.summary_builder import BehaviorSummaryBuilder
from app.calendar_operations.gateway import CalendarWriteGateway
from app.calendar_operations.http_provider import HttpCalendarWriteProvider
from app.calendar_operations.scripted_provider import ScriptedCalendarWriteProvider
from app.calendar_read.gateway import CalendarReadGateway
from app.calendar_read.http_provider import (
    HttpCalendarReadProvider,
    ScriptedCalendarReadProvider,
)
from app.config import Settings, get_settings
from app.context.audit import ContextAuditService
from app.context.budget import ContextBudget
from app.context.builder import DeterministicContextBuilder
from app.context.registry import build_context_contract_registry
from app.context.snapshot import ContextSnapshotService
from app.domain.calendar_operations.protocols import (
    CalendarOperationRepository,
    CalendarWriteProvider,
)
from app.domain.calendar_read.protocols import CalendarReadProvider
from app.domain.checkins.repositories import CheckInRepository
from app.domain.common import LocationType
from app.domain.context.repositories import ContextSnapshotRepository
from app.domain.exercises.catalog_seed import CATALOG_SEED
from app.domain.exercises.repositories import ExerciseRepository
from app.domain.ics.repositories import IcsExportRepository
from app.domain.memory.repositories import MemoryRepository
from app.domain.orchestration.repositories import OrchestrationRepository
from app.domain.plans.repositories import PlanRepository
from app.domain.profiles.models import ConstraintType, FitnessGoal
from app.domain.profiles.repositories import ProfileRepository
from app.domain.recovery.repositories import RecoveryDraftRepository
from app.domain.recovery_application.repositories import RecoveryApplicationRepository
from app.domain.schedule_application.repositories import ScheduleApplicationRepository
from app.domain.scheduling.repositories import ScheduleDraftRepository
from app.domain.session_design.repositories import SessionDesignRepository
from app.domain.session_design_application.repositories import (
    SessionDesignApplicationRepository,
)
from app.domain.users.models import UserAccount
from app.memory.cache import MemoryActiveCache
from app.memory.candidate_service import MemoryCandidateService
from app.memory.metrics import MemoryMetrics
from app.memory.retrieval import MemoryRetriever
from app.memory.service import MemoryService
from app.model_gateway.factory import build_model_gateway
from app.model_gateway.gateway import ModelGateway
from app.observability.facade import ObservabilityFacade, build_observability
from app.observability.safety import ObservedSafetyEngine
from app.orchestration.calendar_operation_workflow_handlers import (
    ExecuteCalendarOperationItemsHandler,
    FinalizeCalendarOperationHandler,
    LoadCalendarOperationDraftHandler,
    ValidateCalendarOperationApprovalHandler,
    VerifyCalendarOperationResultsHandler,
)
from app.orchestration.clock import SystemClock
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
from app.orchestration.worker_pool import OrchestrationWorkerPool
from app.orchestration.workflow_handlers import (
    FinalizeRunHandler,
    GenerateDeterministicPlanHandler,
    LoadProfileContextHandler,
    VerifyPlanSafetyHandler,
    WaitForUserConfirmationHandler,
)
from app.persistence.memory import (
    InMemoryCalendarOperationRepository,
    InMemoryCheckInRepository,
    InMemoryContextSnapshotRepository,
    InMemoryDraftMemoryCandidateImportRepository,
    InMemoryExerciseRepository,
    InMemoryIcsExportRepository,
    InMemoryMemoryRepository,
    InMemoryOrchestrationRepository,
    InMemoryPlanRepository,
    InMemoryProfileAgentDraftRepository,
    InMemoryProfileDraftReviewRepository,
    InMemoryProfileRepository,
    InMemoryRecoveryApplicationRepository,
    InMemoryRecoveryDraftRepository,
    InMemoryScheduleApplicationRepository,
    InMemoryScheduleDraftRepository,
    InMemorySessionDesignApplicationRepository,
    InMemorySessionDesignRepository,
    InMemoryStore,
)
from app.persistence.mysql.calendar_operation_repository import (
    MySQLCalendarOperationRepository,
)
from app.persistence.mysql.context_snapshot_repository import (
    MySQLContextSnapshotRepository,
)
from app.persistence.mysql.ics_export_repository import MySQLIcsExportRepository
from app.persistence.mysql.memory_repository import MySQLMemoryRepository
from app.persistence.mysql.profile_draft_repository import MySQLProfileDraftRepository
from app.persistence.mysql.recovery_application_repository import (
    MySQLRecoveryApplicationRepository,
)
from app.persistence.mysql.recovery_draft_repository import (
    MySQLRecoveryDraftRepository,
)
from app.persistence.mysql.schedule_application_repository import (
    MySQLScheduleApplicationRepository,
)
from app.persistence.mysql.schedule_repository import MySQLScheduleDraftRepository
from app.persistence.mysql.session_design_application_repository import (
    MySQLSessionDesignApplicationRepository,
)
from app.persistence.mysql.session_design_repository import (
    MySQLSessionDesignRepository,
)
from app.planning.generator import DeterministicPlanGenerator
from app.planning.repair import PlanRepairer
from app.profile_application.apply import ProfileDraftApplyService
from app.profile_application.memory_candidates import ProfileDraftMemoryCandidateService
from app.profile_application.merge_policy import ProfileDraftMergePolicy
from app.profile_application.preview import ProfileDraftPreviewService
from app.prompts.profile_agent import PROFILE_AGENT_PROMPT_V1, PROFILE_AGENT_PROMPT_V2
from app.prompts.recovery_agent import RECOVERY_AGENT_PROMPT_V1
from app.prompts.registry import PromptRegistry
from app.prompts.schedule_agent import SCHEDULE_AGENT_PROMPT_V1
from app.prompts.session_designer import SESSION_DESIGNER_PROMPT_V1
from app.recovery.candidate_generator import RecoveryCandidateGenerator
from app.recovery.candidate_set import RecoveryCandidateSetBuilder
from app.recovery.change_impact import RecoveryChangeImpactAnalyzer
from app.recovery.deterministic_fallback import DeterministicRecoveryFallback
from app.recovery.memory_proposals import BehaviorMemoryProposalBuilder
from app.recovery.scope_guard import RecoveryScopeGuard
from app.safety.engine import SafetyEngine
from app.scheduling.candidate_set import TimeSlotCandidateSetBuilder
from app.scheduling.metrics import ScheduleMetrics
from app.session_design.metrics import SessionDesignMetrics
from app.tool_gateway.factory import build_tool_gateway
from app.tool_gateway.gateway import ToolGateway


@dataclass(slots=True)
class BusinessContainer:
    """Own one in-memory store and its services for one application lifespan."""

    store: InMemoryStore
    development_user: UserAccount
    profile_repository: ProfileRepository
    exercise_repository: ExerciseRepository
    plan_repository: PlanRepository
    check_in_repository: CheckInRepository
    safety_engine: SafetyEngine
    profile_service: ProfileService
    exercise_service: ExerciseCatalogService
    plan_service: PlanService
    plan_generation_service: PlanGenerationService
    session_service: SessionService
    session_design_repository: SessionDesignRepository
    session_design_service: SessionDesignService
    session_design_application_repository: SessionDesignApplicationRepository
    session_design_plan_application_service: SessionDesignPlanApplicationService
    session_design_application_run_service: SessionDesignApplicationRunService
    session_design_metrics: SessionDesignMetrics
    schedule_draft_repository: ScheduleDraftRepository
    schedule_draft_service: ScheduleDraftService
    schedule_application_repository: ScheduleApplicationRepository
    schedule_plan_application_service: SchedulePlanApplicationService
    ics_export_repository: IcsExportRepository
    ics_export_service: IcsExportService
    calendar_operation_service: CalendarOperationService
    schedule_application_run_service: ScheduleApplicationRunService
    calendar_operation_run_service: CalendarOperationRunService
    schedule_metrics: ScheduleMetrics
    recovery_draft_repository: RecoveryDraftRepository
    recovery_draft_service: RecoveryDraftService
    recovery_application_repository: RecoveryApplicationRepository
    recovery_application_service: RecoveryApplicationService
    recovery_application_run_service: RecoveryApplicationRunService
    recovery_metrics: RecoveryMetrics
    calendar_read_gateway: CalendarReadGateway
    calendar_write_gateway: CalendarWriteGateway
    check_in_service: CheckInService
    progress_service: ProgressService
    local_replanning_service: LocalReplanningService
    model_gateway: ModelGateway
    profile_agent_service: ProfileAgentService
    profile_draft_preview_service: ProfileDraftPreviewService
    profile_draft_apply_service: ProfileDraftApplyService
    profile_draft_memory_candidate_service: ProfileDraftMemoryCandidateService
    profile_agent_run_service: ProfileAgentRunService
    memory_repository: MemoryRepository
    memory_application_service: MemoryApplicationService
    context_application_service: ContextApplicationService
    context_snapshot_repository: ContextSnapshotRepository
    memory_metrics: MemoryMetrics
    orchestration_repository: OrchestrationRepository
    orchestration_service: OrchestrationService
    orchestrator_metrics: OrchestratorMetrics
    orchestrator_workers: tuple[OrchestrationWorker, ...]
    orchestrator_reaper: StepReaper
    orchestrator_pool: OrchestrationWorkerPool | None
    tool_gateway: ToolGateway
    observability: ObservabilityFacade
    orchestrator_startup_failed: bool = False


@dataclass(frozen=True, slots=True)
class MySQLIcsExportServices:
    """Durable ICS dependencies composed once for the MySQL application path."""

    repository: MySQLIcsExportRepository
    gateway: ToolGateway
    service: IcsExportService


@dataclass(frozen=True, slots=True)
class MySQLCalendarOperationServices:
    """Durable Calendar Draft dependencies composed once for the MySQL path."""

    repository: MySQLCalendarOperationRepository
    gateway: CalendarWriteGateway
    service: CalendarOperationService


@dataclass(frozen=True, slots=True)
class MySQLRecoveryDraftServices:
    """Durable Recovery Draft dependencies owned by one MySQL lifespan."""

    repository: MySQLRecoveryDraftRepository
    service: RecoveryDraftService
    model_gateway: ModelGateway
    tool_gateway: ToolGateway
    metrics: RecoveryMetrics


@dataclass(frozen=True, slots=True)
class MySQLRecoveryApplicationServices:
    """Durable Recovery Application dependencies for API and Worker compositions."""

    repository: MySQLRecoveryApplicationRepository
    service: RecoveryApplicationService


def build_memory_container(
    settings: Settings | None = None,
    *,
    observability: ObservabilityFacade | None = None,
    cache: MemoryActiveCache | None = None,
) -> BusinessContainer:
    """Compose the temporary Phase 1A adapter without external connections."""

    active_settings = settings or get_settings()
    active_observability = observability or build_observability(
        enabled=active_settings.observability_enabled,
        tracing_enabled=active_settings.otel_tracing_enabled,
        exporter_name=active_settings.otel_exporter.value,
        service_name=active_settings.otel_service_name,
        metrics_enabled=active_settings.prometheus_metrics_enabled,
        structured_logging_enabled=active_settings.structured_logging_enabled,
    )
    store = InMemoryStore(CATALOG_SEED)
    profile_repository = InMemoryProfileRepository(store)
    exercise_repository = InMemoryExerciseRepository(store)
    plan_repository = InMemoryPlanRepository(store)
    check_in_repository = InMemoryCheckInRepository(store)
    session_design_repository = InMemorySessionDesignRepository(store)
    schedule_draft_repository = InMemoryScheduleDraftRepository(store)
    schedule_application_repository = InMemoryScheduleApplicationRepository(store)
    ics_export_repository = InMemoryIcsExportRepository(store)
    calendar_operation_repository = InMemoryCalendarOperationRepository(store)
    recovery_draft_repository = InMemoryRecoveryDraftRepository(store)
    recovery_application_repository = InMemoryRecoveryApplicationRepository(store)
    session_design_application_repository = InMemorySessionDesignApplicationRepository(
        store
    )
    memory_repository = InMemoryMemoryRepository(store)
    context_snapshot_repository = InMemoryContextSnapshotRepository(store)
    draft_memory_candidate_import_repository = (
        InMemoryDraftMemoryCandidateImportRepository(store)
    )
    memory_metrics = MemoryMetrics()
    memory_service = MemoryService(memory_repository, memory_metrics)
    candidate_service = MemoryCandidateService(
        memory_repository,
        memory_service,
        memory_metrics,
    )
    memory_application_service = MemoryApplicationService(
        memory_service,
        candidate_service,
        observability=active_observability,
    )
    context_registry = build_context_contract_registry()
    memory_retriever = MemoryRetriever(memory_repository, memory_metrics)
    context_builder = DeterministicContextBuilder(
        profiles=profile_repository,
        memories=memory_repository,
        retriever=memory_retriever,
        metrics=memory_metrics,
        budget=ContextBudget(
            max_characters=active_settings.context_max_characters,
            max_memories=active_settings.context_max_memories,
            max_behavior_items=active_settings.context_max_behavior_items,
        ),
        registry=context_registry,
    )
    context_snapshot_service = ContextSnapshotService(
        builder=context_builder,
        registry=context_registry,
        snapshots=context_snapshot_repository,
        profiles=profile_repository,
        memories=memory_repository,
        metrics=memory_metrics,
    )
    context_application_service = ContextApplicationService(
        context_builder,
        ContextAuditService(memory_repository),
        context_snapshot_service,
        active_observability,
    )
    safety_engine = ObservedSafetyEngine(SafetyEngine(), active_observability)
    plan_generator = DeterministicPlanGenerator()
    model_gateway = build_model_gateway(
        active_settings,
        observability=active_observability,
    )
    prompt_registry = PromptRegistry()
    prompt_registry.register(PROFILE_AGENT_PROMPT_V1)
    prompt_registry.register(PROFILE_AGENT_PROMPT_V2)
    prompt_registry.register(SESSION_DESIGNER_PROMPT_V1)
    prompt_registry.register(SCHEDULE_AGENT_PROMPT_V1)
    prompt_registry.register(RECOVERY_AGENT_PROMPT_V1)
    profile_agent = ProfileAgent(
        gateway=model_gateway,
        prompts=prompt_registry,
        prompt_version="profile-agent-v2",
        request_timeout_seconds=active_settings.model_request_timeout_seconds,
        max_response_bytes=active_settings.model_max_response_bytes,
    )
    profile_agent_drafts = InMemoryProfileAgentDraftRepository(store)
    session_design_metrics = SessionDesignMetrics()
    session_designer_agent = SessionDesignerAgent(
        gateway=model_gateway,
        prompts=prompt_registry,
        request_timeout_seconds=active_settings.model_request_timeout_seconds,
        max_response_bytes=active_settings.model_max_response_bytes,
    )
    schedule_agent = ScheduleAgent(
        gateway=model_gateway,
        prompts=prompt_registry,
        request_timeout_seconds=active_settings.model_request_timeout_seconds,
        max_response_bytes=active_settings.model_max_response_bytes,
    )
    recovery_agent = RecoveryAgent(
        gateway=model_gateway,
        prompts=prompt_registry,
        request_timeout_seconds=active_settings.model_request_timeout_seconds,
        max_response_bytes=active_settings.model_max_response_bytes,
    )
    profile_draft_memory_candidates = ProfileDraftMemoryCandidateService(
        drafts=profile_agent_drafts,
        memories=memory_application_service,
        imports=draft_memory_candidate_import_repository,
        metrics=memory_metrics,
    )
    profile_draft_reviews = InMemoryProfileDraftReviewRepository(store)
    profile_draft_previews = ProfileDraftPreviewService(
        reviews=profile_draft_reviews,
        profiles=profile_repository,
        merge_policy=ProfileDraftMergePolicy(),
    )
    profile_draft_applications = ProfileDraftApplyService(
        reviews=profile_draft_reviews,
        previews=profile_draft_previews,
    )
    development_user = build_local_user(active_settings)
    profile_service = ProfileService(profile_repository)
    exercise_service = ExerciseCatalogService(exercise_repository)
    plan_service = PlanService(
        profiles=profile_repository,
        exercises=exercise_repository,
        plans=plan_repository,
        safety_engine=safety_engine,
    )
    plan_generation_service = PlanGenerationService(
        profiles=profile_repository,
        exercises=exercise_repository,
        plans=plan_repository,
        safety_engine=safety_engine,
        generator=plan_generator,
        repairer=PlanRepairer(plan_generator),
        contexts=context_application_service,
        memory_metrics=memory_metrics,
    )
    profile_agent_service = ProfileAgentService(
        profiles=profile_repository,
        drafts=profile_agent_drafts,
        agent=profile_agent,
        gateway=model_gateway,
        contexts=context_application_service,
        memory_metrics=memory_metrics,
        enabled=active_settings.model_gateway_enabled,
        supported_goals=tuple(item.value for item in FitnessGoal),
        supported_constraint_types=tuple(item.value for item in ConstraintType),
        supported_equipment=tuple(
            sorted(
                {
                    equipment
                    for exercise in CATALOG_SEED
                    for equipment in exercise.required_equipment
                }
            )
        ),
        supported_locations=tuple(item.value for item in LocationType),
    )
    session_design_service = SessionDesignService(
        profiles=profile_repository,
        exercises=exercise_repository,
        drafts=session_design_repository,
        contexts=context_application_service,
        agent=session_designer_agent,
        safety=safety_engine,
        metrics=session_design_metrics,
        enabled=active_settings.model_gateway_enabled,
    )
    session_design_plan_application_service = SessionDesignPlanApplicationService(
        profiles=profile_repository,
        exercises=exercise_repository,
        plans=plan_repository,
        check_ins=check_in_repository,
        drafts=session_design_repository,
        contexts=context_snapshot_repository,
        applications=session_design_application_repository,
        safety=safety_engine,
    )
    local_replanning_service = LocalReplanningService(
        profiles=profile_repository,
        exercises=exercise_repository,
        plans=plan_repository,
        check_ins=check_in_repository,
        safety_engine=safety_engine,
    )
    metrics = OrchestratorMetrics()
    orchestration_repository = InMemoryOrchestrationRepository(metrics)
    clock = SystemClock()
    recovery_metrics = RecoveryMetrics()
    behavior_summary_builder = BehaviorSummaryBuilder(
        clock=clock,
        metrics=recovery_metrics,
        default_window_days=active_settings.behavior_summary_default_window_days,
        max_window_days=active_settings.behavior_summary_max_window_days,
        min_signal_occurrences=(
            active_settings.behavior_summary_min_signal_occurrences
        ),
        repeat_ratio_threshold=Decimal(
            str(active_settings.behavior_summary_repeat_ratio_threshold)
        ),
        min_rpe_samples=active_settings.behavior_summary_min_rpe_samples,
    )
    calendar_provider: CalendarReadProvider | None = None
    if active_settings.calendar_read_provider == "scripted":
        calendar_provider = ScriptedCalendarReadProvider()
    elif active_settings.calendar_read_provider == "http":
        if (
            active_settings.calendar_read_base_url is None
            or active_settings.calendar_read_api_key is None
        ):
            raise ValueError("Calendar HTTP provider configuration is incomplete")
        calendar_provider = HttpCalendarReadProvider(
            base_url=active_settings.calendar_read_base_url.get_secret_value(),
            api_key=active_settings.calendar_read_api_key.get_secret_value(),
            timeout_seconds=active_settings.calendar_read_timeout_seconds,
            max_response_bytes=active_settings.calendar_read_max_response_bytes,
        )
    calendar_write_provider = _build_configured_calendar_write_provider(active_settings)
    tool_gateway = build_tool_gateway(
        tuple(exercise.id for exercise in CATALOG_SEED),
        exercise_repository=exercise_repository,
        memory_candidate_service=candidate_service,
        calendar_read_provider=calendar_provider,
        calendar_write_provider=calendar_write_provider,
        calendar_read_timeout_ms=int(
            active_settings.calendar_read_timeout_seconds * 1000
        ),
        calendar_read_attempts=active_settings.calendar_read_max_attempts,
        calendar_write_timeout_ms=int(
            active_settings.calendar_write_timeout_seconds * 1000
        ),
        calendar_write_attempts=active_settings.calendar_write_max_attempts,
        calendar_read_bulkhead_limit=(active_settings.calendar_read_bulkhead_limit),
        circuit_failure_threshold=(active_settings.tool_circuit_failure_threshold),
        circuit_failure_window_seconds=(
            active_settings.tool_circuit_failure_window_seconds
        ),
        circuit_open_duration_seconds=(
            active_settings.tool_circuit_open_duration_seconds
        ),
        retry_budget_maximum_attempts=(
            active_settings.tool_retry_budget_maximum_attempts
        ),
        observability=active_observability,
    )
    plan_generation_service.attach_tool_gateway(tool_gateway)
    session_design_service.attach_tool_gateway(tool_gateway)
    memory_application_service.attach_tool_gateway(tool_gateway)
    memory_retriever.attach_reliability(tool_gateway.traces, tool_gateway.metrics)
    memory_retriever.attach_observability(active_observability)
    calendar_gateway = CalendarReadGateway(
        provider=calendar_provider,
        enabled=active_settings.calendar_read_enabled,
        timeout_seconds=active_settings.calendar_read_timeout_seconds,
        max_attempts=active_settings.calendar_read_max_attempts,
        tool_gateway=tool_gateway,
        user_id=development_user.id,
    )
    calendar_write_gateway = CalendarWriteGateway(
        provider=calendar_write_provider,
        enabled=active_settings.calendar_write_enabled,
        timeout_seconds=active_settings.calendar_write_timeout_seconds,
        # This determines one invocation's total HTTP deadline. The Gateway owns
        # these HTTP attempts; CalendarOperationService separately counts Item
        # executions for worker recovery.
        max_attempts=active_settings.calendar_write_max_attempts,
        tool_gateway=tool_gateway,
        user_id=development_user.id,
    )
    schedule_metrics = ScheduleMetrics()
    schedule_draft_service = ScheduleDraftService(
        profiles=profile_repository,
        plans=plan_repository,
        check_ins=check_in_repository,
        drafts=schedule_draft_repository,
        contexts=context_application_service,
        calendar=calendar_gateway,
        agent=schedule_agent,
        clock=clock,
        metrics=schedule_metrics,
        enabled=(
            active_settings.model_gateway_enabled
            and active_settings.schedule_agent_enabled
        ),
        builder=TimeSlotCandidateSetBuilder(
            granularity_minutes=active_settings.schedule_slot_granularity_minutes,
            buffer_minutes=active_settings.schedule_min_buffer_minutes,
            max_per_session=active_settings.schedule_max_candidates_per_session,
            max_total=active_settings.schedule_max_total_candidates,
        ),
        ttl_minutes=active_settings.schedule_draft_ttl_minutes,
    )
    schedule_plan_application_service = SchedulePlanApplicationService(
        profiles=profile_repository,
        exercises=exercise_repository,
        plans=plan_repository,
        check_ins=check_in_repository,
        drafts=schedule_draft_repository,
        contexts=context_snapshot_repository,
        applications=schedule_application_repository,
        calendar=calendar_gateway,
        safety=safety_engine,
        clock=clock,
        busy_snapshot_max_age_seconds=(
            active_settings.schedule_busy_snapshot_max_age_seconds
        ),
    )
    ics_export_service = IcsExportService(
        plans=plan_repository,
        exports=ics_export_repository,
        clock=clock,
        tool_gateway=tool_gateway,
    )
    calendar_operation_service = CalendarOperationService(
        plans=plan_repository,
        operations=calendar_operation_repository,
        gateway=calendar_write_gateway,
        clock=clock,
        max_attempts=active_settings.calendar_write_max_attempts,
    )
    recovery_draft_service = RecoveryDraftService(
        profiles=profile_repository,
        plans=plan_repository,
        check_ins=check_in_repository,
        calendar_operations=calendar_operation_repository,
        drafts=recovery_draft_repository,
        contexts=context_application_service,
        behavior=behavior_summary_builder,
        impact=RecoveryChangeImpactAnalyzer(clock),
        candidates=RecoveryCandidateGenerator(),
        candidate_sets=RecoveryCandidateSetBuilder(clock),
        proposals=BehaviorMemoryProposalBuilder(clock, recovery_metrics),
        agent=recovery_agent,
        scope_guard=RecoveryScopeGuard(),
        fallback=DeterministicRecoveryFallback(),
        clock=clock,
        metrics=recovery_metrics,
        enabled=(
            active_settings.model_gateway_enabled
            and active_settings.recovery_agent_enabled
        ),
        ttl_minutes=active_settings.recovery_draft_ttl_minutes,
    )
    recovery_draft_service.attach_tool_gateway(tool_gateway)
    recovery_application_service = RecoveryApplicationService(
        drafts=recovery_draft_repository,
        applications=recovery_application_repository,
        plans=plan_repository,
        profiles=profile_repository,
        exercises=exercise_repository,
        check_ins=check_in_repository,
        session_designs=session_design_repository,
        schedules=schedule_draft_repository,
        session_design_service=session_design_service,
        schedule_service=schedule_draft_service,
        memories=memory_application_service,
        calendar_operations=calendar_operation_service,
        safety=safety_engine,
        tool_gateway=tool_gateway,
    )
    retry_policy = RetryPolicy()
    orchestration_service = OrchestrationService(
        repository=orchestration_repository,
        plan_service=plan_service,
        clock=clock,
        retry_policy=retry_policy,
        metrics=metrics,
        enabled=active_settings.orchestrator_enabled,
    )
    profile_agent_run_service = ProfileAgentRunService(
        repository=orchestration_repository,
        apply_service=profile_draft_applications,
        clock=clock,
        retry_policy=retry_policy,
        enabled=active_settings.orchestrator_enabled,
    )
    session_design_application_run_service = SessionDesignApplicationRunService(
        repository=orchestration_repository,
        drafts=session_design_repository,
        revisions=local_replanning_service,
        clock=clock,
        retry_policy=retry_policy,
        enabled=active_settings.orchestrator_enabled,
    )
    schedule_application_run_service = ScheduleApplicationRunService(
        repository=orchestration_repository,
        drafts=schedule_draft_repository,
        revisions=local_replanning_service,
        clock=clock,
        retry_policy=retry_policy,
        enabled=active_settings.orchestrator_enabled,
    )
    calendar_operation_run_service = CalendarOperationRunService(
        repository=orchestration_repository,
        operations=calendar_operation_service,
        clock=clock,
        retry_policy=retry_policy,
        enabled=active_settings.orchestrator_enabled,
    )
    recovery_application_run_service = RecoveryApplicationRunService(
        repository=orchestration_repository,
        drafts=recovery_draft_repository,
        session_designs=session_design_repository,
        schedules=schedule_draft_repository,
        revisions=local_replanning_service,
        clock=clock,
        retry_policy=retry_policy,
        enabled=active_settings.orchestrator_enabled,
    )
    registry = HandlerRegistry()
    for handler in (
        LoadProfileContextHandler(
            profiles=profile_repository,
            exercises=exercise_repository,
            contexts=context_application_service,
            user=development_user,
        ),
        GenerateDeterministicPlanHandler(
            service=plan_generation_service,
            user=development_user,
        ),
        VerifyPlanSafetyHandler(
            profiles=profile_repository,
            exercises=exercise_repository,
            plans=plan_repository,
            safety_engine=safety_engine,
        ),
        WaitForUserConfirmationHandler(),
        FinalizeRunHandler(plans=plan_repository),
        ParseProfileRequestHandler(
            service=profile_agent_service,
            user=development_user,
        ),
        WaitForProfileDraftReviewHandler(),
        ApplyProfileDraftHandler(
            service=profile_draft_applications,
            user=development_user,
        ),
        FinalizeProfileRunHandler(
            reviews=profile_draft_reviews,
            profiles=profile_repository,
            apply_service=profile_draft_applications,
            user=development_user,
        ),
        LoadSessionApplicationContextHandler(
            drafts=session_design_repository,
            contexts=context_snapshot_repository,
            plans=plan_repository,
            user=development_user,
        ),
        ValidateSessionDesignTargetHandler(
            service=session_design_plan_application_service,
            user=development_user,
        ),
        BuildSessionPlanRevisionHandler(
            service=session_design_plan_application_service,
            user=development_user,
        ),
        VerifySessionPlanSafetyHandler(
            service=session_design_plan_application_service,
            user=development_user,
        ),
        WaitForPlanRevisionConfirmationHandler(),
        FinalizeSessionApplicationHandler(
            service=session_design_plan_application_service,
            plans=plan_repository,
            user=development_user,
        ),
        LoadScheduleApplicationContextHandler(
            schedule_draft_repository, development_user
        ),
        ValidateScheduleApplicationHandler(
            schedule_plan_application_service, development_user
        ),
        RevalidateCalendarBusyHandler(
            schedule_plan_application_service, development_user
        ),
        BuildSchedulePlanRevisionHandler(
            schedule_plan_application_service, development_user
        ),
        VerifySchedulePlanSafetyHandler(
            schedule_plan_application_service, development_user
        ),
        WaitForScheduleRevisionConfirmationHandler(),
        FinalizeScheduleApplicationHandler(
            schedule_plan_application_service,
            plan_repository,
            development_user,
        ),
        LoadCalendarOperationDraftHandler(calendar_operation_service, development_user),
        ValidateCalendarOperationApprovalHandler(
            calendar_operation_service, development_user
        ),
        ExecuteCalendarOperationItemsHandler(
            calendar_operation_service, development_user
        ),
        VerifyCalendarOperationResultsHandler(
            calendar_operation_service, development_user
        ),
        FinalizeCalendarOperationHandler(calendar_operation_service, development_user),
        LoadRecoveryApplicationContextHandler(
            recovery_application_service, development_user
        ),
        ValidateRecoveryDraftHandler(recovery_application_service, development_user),
        ResolveRecoveryActionsHandler(recovery_application_service, development_user),
        CreateRecoverySubdraftsHandler(recovery_application_service, development_user),
        WaitForRecoverySubdraftReviewsHandler(
            session_designs=session_design_repository,
            schedules=schedule_draft_repository,
            user=development_user,
        ),
        BuildRecoveryPlanRevisionHandler(
            recovery_application_service, development_user
        ),
        VerifyRecoveryPlanSafetyHandler(recovery_application_service, development_user),
        WaitForRecoveryRevisionConfirmationHandler(),
        FinalizeRecoveryApplicationHandler(
            service=recovery_application_service,
            plans=plan_repository,
            user=development_user,
        ),
    ):
        registry.register(handler)
    workers = tuple(
        OrchestrationWorker(
            worker_id=f"worker-{index + 1:02d}",
            repository=orchestration_repository,
            registry=registry,
            clock=clock,
            retry_policy=retry_policy,
            lease_duration=timedelta(
                seconds=active_settings.orchestrator_lease_seconds
            ),
            handler_timeout_seconds=(
                active_settings.orchestrator_handler_timeout_seconds
            ),
            poll_interval_seconds=(active_settings.orchestrator_poll_interval_seconds),
            observability=active_observability,
        )
        for index in range(active_settings.orchestrator_worker_count)
    )
    orchestrator_reaper = StepReaper(
        repository=orchestration_repository,
        clock=clock,
        retry_policy=retry_policy,
    )
    orchestrator_pool = (
        OrchestrationWorkerPool(
            workers=workers,
            reaper=orchestrator_reaper,
            reaper_interval_seconds=(
                active_settings.orchestrator_reaper_interval_seconds
            ),
        )
        if active_settings.orchestrator_enabled
        else None
    )
    return BusinessContainer(
        store=store,
        development_user=development_user,
        profile_repository=profile_repository,
        exercise_repository=exercise_repository,
        plan_repository=plan_repository,
        check_in_repository=check_in_repository,
        safety_engine=safety_engine,
        profile_service=profile_service,
        exercise_service=exercise_service,
        plan_service=plan_service,
        plan_generation_service=plan_generation_service,
        session_service=SessionService(plan_repository),
        session_design_repository=session_design_repository,
        session_design_service=session_design_service,
        session_design_application_repository=(session_design_application_repository),
        session_design_plan_application_service=(
            session_design_plan_application_service
        ),
        session_design_application_run_service=(session_design_application_run_service),
        session_design_metrics=session_design_metrics,
        schedule_draft_repository=schedule_draft_repository,
        schedule_draft_service=schedule_draft_service,
        schedule_application_repository=schedule_application_repository,
        schedule_plan_application_service=schedule_plan_application_service,
        ics_export_repository=ics_export_repository,
        ics_export_service=ics_export_service,
        calendar_operation_service=calendar_operation_service,
        schedule_application_run_service=schedule_application_run_service,
        calendar_operation_run_service=calendar_operation_run_service,
        schedule_metrics=schedule_metrics,
        recovery_draft_repository=recovery_draft_repository,
        recovery_draft_service=recovery_draft_service,
        recovery_application_repository=recovery_application_repository,
        recovery_application_service=recovery_application_service,
        recovery_application_run_service=recovery_application_run_service,
        recovery_metrics=recovery_metrics,
        calendar_read_gateway=calendar_gateway,
        calendar_write_gateway=calendar_write_gateway,
        check_in_service=CheckInService(
            plans=plan_repository,
            check_ins=check_in_repository,
        ),
        progress_service=ProgressService(
            plans=plan_repository,
            check_ins=check_in_repository,
        ),
        local_replanning_service=local_replanning_service,
        model_gateway=model_gateway,
        profile_agent_service=profile_agent_service,
        profile_draft_preview_service=profile_draft_previews,
        profile_draft_apply_service=profile_draft_applications,
        profile_draft_memory_candidate_service=profile_draft_memory_candidates,
        profile_agent_run_service=profile_agent_run_service,
        memory_repository=memory_repository,
        memory_application_service=memory_application_service,
        context_application_service=context_application_service,
        context_snapshot_repository=context_snapshot_repository,
        memory_metrics=memory_metrics,
        orchestration_repository=orchestration_repository,
        orchestration_service=orchestration_service,
        orchestrator_metrics=metrics,
        orchestrator_workers=workers,
        orchestrator_reaper=orchestrator_reaper,
        orchestrator_pool=orchestrator_pool,
        tool_gateway=tool_gateway,
        observability=active_observability,
    )


def build_mysql_memory_services(
    settings: Settings,
    *,
    profiles: ProfileRepository,
    sessions: async_sessionmaker[AsyncSession],
    observability: ObservabilityFacade | None = None,
    cache: MemoryActiveCache | None = None,
) -> tuple[
    MemoryApplicationService,
    ContextApplicationService,
    MySQLMemoryRepository,
    MySQLContextSnapshotRepository,
]:
    """Compose the durable Memory/Context path without an in-memory container."""

    # The Database boundary owns this session factory; repositories create a short
    # session and transaction per operation, never retaining an AsyncSession.
    memory_repository = MySQLMemoryRepository(sessions)
    snapshot_repository = MySQLContextSnapshotRepository(sessions)
    metrics = MemoryMetrics()
    memory_service = MemoryService(memory_repository, metrics, cache)
    candidate_service = MemoryCandidateService(
        memory_repository,
        memory_service,
        metrics,
    )
    memory_application_service = MemoryApplicationService(
        memory_service,
        candidate_service,
        observability=observability,
    )
    registry = build_context_contract_registry()
    builder = DeterministicContextBuilder(
        profiles=profiles,
        memories=memory_repository,
        retriever=MemoryRetriever(memory_repository, metrics, cache),
        metrics=metrics,
        budget=ContextBudget(
            max_characters=settings.context_max_characters,
            max_memories=settings.context_max_memories,
            max_behavior_items=settings.context_max_behavior_items,
        ),
        registry=registry,
    )
    snapshots = ContextSnapshotService(
        builder=builder,
        registry=registry,
        snapshots=snapshot_repository,
        profiles=profiles,
        memories=memory_repository,
        metrics=metrics,
    )
    context_application_service = ContextApplicationService(
        builder,
        ContextAuditService(memory_repository),
        snapshots,
        observability=observability,
    )
    return (
        memory_application_service,
        context_application_service,
        memory_repository,
        snapshot_repository,
    )


def build_mysql_ics_export_services(
    settings: Settings,
    *,
    plans: PlanRepository,
    exercises: ExerciseRepository,
    sessions: async_sessionmaker[AsyncSession],
    observability: ObservabilityFacade | None = None,
) -> MySQLIcsExportServices:
    """Compose MySQL ICS persistence, typed Gateway, and service once."""

    repository = MySQLIcsExportRepository(sessions)
    gateway = build_tool_gateway(
        tuple(exercise.id for exercise in CATALOG_SEED),
        exercise_repository=exercises,
        calendar_read_timeout_ms=int(settings.calendar_read_timeout_seconds * 1000),
        calendar_read_attempts=settings.calendar_read_max_attempts,
        calendar_write_timeout_ms=int(settings.calendar_write_timeout_seconds * 1000),
        calendar_write_attempts=settings.calendar_write_max_attempts,
        calendar_read_bulkhead_limit=settings.calendar_read_bulkhead_limit,
        circuit_failure_threshold=settings.tool_circuit_failure_threshold,
        circuit_failure_window_seconds=settings.tool_circuit_failure_window_seconds,
        circuit_open_duration_seconds=settings.tool_circuit_open_duration_seconds,
        retry_budget_maximum_attempts=settings.tool_retry_budget_maximum_attempts,
        observability=observability,
    )
    return MySQLIcsExportServices(
        repository=repository,
        gateway=gateway,
        service=IcsExportService(
            plans=plans,
            exports=repository,
            clock=SystemClock(),
            tool_gateway=gateway,
        ),
    )


def build_mysql_calendar_operation_services(
    settings: Settings,
    *,
    plans: PlanRepository,
    sessions: async_sessionmaker[AsyncSession],
    clock: SystemClock,
) -> MySQLCalendarOperationServices:
    """Compose durable Calendar Draft review state with an API-side write deny."""

    repository = MySQLCalendarOperationRepository(sessions)
    gateway = CalendarWriteGateway(
        provider=None,
        enabled=False,
        timeout_seconds=settings.calendar_write_timeout_seconds,
        max_attempts=settings.calendar_write_max_attempts,
    )
    return MySQLCalendarOperationServices(
        repository=repository,
        gateway=gateway,
        service=CalendarOperationService(
            plans=plans,
            operations=repository,
            gateway=gateway,
            clock=clock,
            max_attempts=settings.calendar_write_max_attempts,
        ),
    )


def build_mysql_recovery_draft_services(
    settings: Settings,
    *,
    profiles: ProfileRepository,
    exercises: ExerciseRepository,
    plans: PlanRepository,
    check_ins: CheckInRepository,
    calendar_operations: CalendarOperationRepository,
    contexts: ContextApplicationService,
    sessions: async_sessionmaker[AsyncSession],
    observability: ObservabilityFacade | None = None,
) -> MySQLRecoveryDraftServices:
    """Compose durable Phase 7A Recovery without a memory fallback."""

    repository = MySQLRecoveryDraftRepository(sessions)
    model_gateway = build_model_gateway(settings, observability=observability)
    prompts = PromptRegistry()
    prompts.register(RECOVERY_AGENT_PROMPT_V1)
    agent = RecoveryAgent(
        gateway=model_gateway,
        prompts=prompts,
        request_timeout_seconds=settings.model_request_timeout_seconds,
        max_response_bytes=settings.model_max_response_bytes,
    )
    tool_gateway = build_tool_gateway(
        tuple(exercise.id for exercise in CATALOG_SEED),
        exercise_repository=exercises,
        calendar_read_timeout_ms=int(settings.calendar_read_timeout_seconds * 1000),
        calendar_read_attempts=settings.calendar_read_max_attempts,
        calendar_write_timeout_ms=int(settings.calendar_write_timeout_seconds * 1000),
        calendar_write_attempts=settings.calendar_write_max_attempts,
        calendar_read_bulkhead_limit=settings.calendar_read_bulkhead_limit,
        circuit_failure_threshold=settings.tool_circuit_failure_threshold,
        circuit_failure_window_seconds=settings.tool_circuit_failure_window_seconds,
        circuit_open_duration_seconds=settings.tool_circuit_open_duration_seconds,
        retry_budget_maximum_attempts=settings.tool_retry_budget_maximum_attempts,
        observability=observability,
    )
    clock = SystemClock()
    metrics = RecoveryMetrics()
    service = RecoveryDraftService(
        profiles=profiles,
        plans=plans,
        check_ins=check_ins,
        calendar_operations=calendar_operations,
        drafts=repository,
        contexts=contexts,
        behavior=BehaviorSummaryBuilder(
            clock=clock,
            metrics=metrics,
            default_window_days=settings.behavior_summary_default_window_days,
            max_window_days=settings.behavior_summary_max_window_days,
            min_signal_occurrences=(settings.behavior_summary_min_signal_occurrences),
            repeat_ratio_threshold=Decimal(
                str(settings.behavior_summary_repeat_ratio_threshold)
            ),
            min_rpe_samples=settings.behavior_summary_min_rpe_samples,
        ),
        impact=RecoveryChangeImpactAnalyzer(clock),
        candidates=RecoveryCandidateGenerator(),
        candidate_sets=RecoveryCandidateSetBuilder(clock),
        proposals=BehaviorMemoryProposalBuilder(clock, metrics),
        agent=agent,
        scope_guard=RecoveryScopeGuard(),
        fallback=DeterministicRecoveryFallback(),
        clock=clock,
        metrics=metrics,
        enabled=(settings.model_gateway_enabled and settings.recovery_agent_enabled),
        ttl_minutes=settings.recovery_draft_ttl_minutes,
    )
    service.attach_tool_gateway(tool_gateway)
    return MySQLRecoveryDraftServices(
        repository=repository,
        service=service,
        model_gateway=model_gateway,
        tool_gateway=tool_gateway,
        metrics=metrics,
    )


def build_mysql_recovery_application_services(
    *,
    recovery_drafts: RecoveryDraftRepository,
    profiles: ProfileRepository,
    exercises: ExerciseRepository,
    plans: PlanRepository,
    check_ins: CheckInRepository,
    session_designs: SessionDesignRepository,
    schedules: ScheduleDraftRepository,
    session_design_service: SessionDesignService,
    schedule_service: ScheduleDraftService,
    memories: MemoryApplicationService,
    calendar_operations: CalendarOperationService,
    sessions: async_sessionmaker[AsyncSession],
    safety: SafetyEngine,
    tool_gateway: ToolGateway,
) -> MySQLRecoveryApplicationServices:
    """Compose Recovery Application exclusively from durable MySQL adapters."""

    repository = MySQLRecoveryApplicationRepository(sessions)
    return MySQLRecoveryApplicationServices(
        repository=repository,
        service=RecoveryApplicationService(
            drafts=recovery_drafts,
            applications=repository,
            plans=plans,
            profiles=profiles,
            exercises=exercises,
            check_ins=check_ins,
            session_designs=session_designs,
            schedules=schedules,
            session_design_service=session_design_service,
            schedule_service=schedule_service,
            memories=memories,
            calendar_operations=calendar_operations,
            safety=safety,
            tool_gateway=tool_gateway,
        ),
    )


def _build_configured_calendar_write_provider(
    settings: Settings,
) -> CalendarWriteProvider | None:
    """Build the configured write Provider for executor-owned compositions."""

    if settings.calendar_write_provider == "scripted":
        return ScriptedCalendarWriteProvider()
    if settings.calendar_write_provider == "http":
        if (
            settings.calendar_write_base_url is None
            or settings.calendar_write_api_key is None
        ):
            raise ValueError("Calendar write HTTP configuration is incomplete")
        return HttpCalendarWriteProvider(
            base_url=settings.calendar_write_base_url.get_secret_value(),
            api_key=settings.calendar_write_api_key.get_secret_value(),
            timeout_seconds=settings.calendar_write_timeout_seconds,
            max_response_bytes=settings.calendar_write_max_response_bytes,
        )
    return None


def build_mysql_calendar_executor_services(
    settings: Settings,
    *,
    user: UserAccount,
    plans: PlanRepository,
    exercises: ExerciseRepository,
    sessions: async_sessionmaker[AsyncSession],
    clock: SystemClock,
) -> MySQLCalendarOperationServices:
    """Compose the enabled Calendar write capability for an owned CLI executor."""

    repository = MySQLCalendarOperationRepository(sessions)
    provider = _build_configured_calendar_write_provider(settings)
    gateway = CalendarWriteGateway(
        provider=provider,
        enabled=settings.calendar_write_enabled,
        timeout_seconds=settings.calendar_write_timeout_seconds,
        max_attempts=settings.calendar_write_max_attempts,
        tool_gateway=build_tool_gateway(
            tuple(exercise.id for exercise in CATALOG_SEED),
            exercise_repository=exercises,
            calendar_write_provider=provider,
            calendar_write_timeout_ms=int(
                settings.calendar_write_timeout_seconds * 1000
            ),
            calendar_write_attempts=settings.calendar_write_max_attempts,
            calendar_read_bulkhead_limit=settings.calendar_read_bulkhead_limit,
            circuit_failure_threshold=settings.tool_circuit_failure_threshold,
            circuit_failure_window_seconds=settings.tool_circuit_failure_window_seconds,
            circuit_open_duration_seconds=settings.tool_circuit_open_duration_seconds,
            retry_budget_maximum_attempts=settings.tool_retry_budget_maximum_attempts,
        ),
        user_id=user.id,
    )
    return MySQLCalendarOperationServices(
        repository=repository,
        gateway=gateway,
        service=CalendarOperationService(
            plans=plans,
            operations=repository,
            gateway=gateway,
            clock=clock,
            max_attempts=settings.calendar_write_max_attempts,
        ),
    )


def build_mysql_profile_agent_service(
    settings: Settings,
    *,
    profiles: ProfileRepository,
    contexts: ContextApplicationService,
    drafts: MySQLProfileDraftRepository,
    observability: ObservabilityFacade | None = None,
) -> tuple[ProfileAgentService, ModelGateway]:
    """Compose the Profile Agent with durable Context and MySQL drafts."""

    gateway = build_model_gateway(settings, observability=observability)
    prompts = PromptRegistry()
    prompts.register(PROFILE_AGENT_PROMPT_V1)
    prompts.register(PROFILE_AGENT_PROMPT_V2)
    agent = ProfileAgent(
        gateway=gateway,
        prompts=prompts,
        prompt_version="profile-agent-v2",
        request_timeout_seconds=settings.model_request_timeout_seconds,
        max_response_bytes=settings.model_max_response_bytes,
    )
    service = ProfileAgentService(
        profiles=profiles,
        drafts=drafts,
        agent=agent,
        gateway=gateway,
        contexts=contexts,
        memory_metrics=MemoryMetrics(),
        enabled=settings.model_gateway_enabled,
        supported_goals=tuple(item.value for item in FitnessGoal),
        supported_constraint_types=tuple(item.value for item in ConstraintType),
        supported_equipment=tuple(
            sorted(
                {
                    equipment
                    for exercise in CATALOG_SEED
                    for equipment in exercise.required_equipment
                }
            )
        ),
        supported_locations=tuple(item.value for item in LocationType),
    )
    return service, gateway


def build_mysql_session_design_services(
    settings: Settings,
    *,
    profiles: ProfileRepository,
    exercises: ExerciseRepository,
    plans: PlanRepository,
    check_ins: CheckInRepository,
    contexts: ContextApplicationService,
    context_snapshots: ContextSnapshotRepository,
    sessions: async_sessionmaker[AsyncSession],
    safety: SafetyEngine,
    observability: ObservabilityFacade | None = None,
) -> tuple[
    SessionDesignService,
    SessionDesignPlanApplicationService,
    MySQLSessionDesignRepository,
    MySQLSessionDesignApplicationRepository,
    ModelGateway,
]:
    """Compose durable Session Design review and atomic application services."""

    drafts = MySQLSessionDesignRepository(sessions)
    applications = MySQLSessionDesignApplicationRepository(sessions)
    gateway = build_model_gateway(settings, observability=observability)
    prompts = PromptRegistry()
    prompts.register(SESSION_DESIGNER_PROMPT_V1)
    agent = SessionDesignerAgent(
        gateway=gateway,
        prompts=prompts,
        request_timeout_seconds=settings.model_request_timeout_seconds,
        max_response_bytes=settings.model_max_response_bytes,
    )
    service = SessionDesignService(
        profiles=profiles,
        exercises=exercises,
        drafts=drafts,
        contexts=contexts,
        agent=agent,
        safety=safety,
        metrics=SessionDesignMetrics(),
        enabled=settings.model_gateway_enabled,
    )
    application = SessionDesignPlanApplicationService(
        profiles=profiles,
        exercises=exercises,
        plans=plans,
        check_ins=check_ins,
        drafts=drafts,
        contexts=context_snapshots,
        applications=applications,
        safety=safety,
    )
    return service, application, drafts, applications, gateway


def build_mysql_schedule_services(
    settings: Settings,
    *,
    user: UserAccount,
    profiles: ProfileRepository,
    exercises: ExerciseRepository,
    plans: PlanRepository,
    check_ins: CheckInRepository,
    contexts: ContextApplicationService,
    context_snapshots: ContextSnapshotRepository,
    sessions: async_sessionmaker[AsyncSession],
    safety: SafetyEngine,
    observability: ObservabilityFacade | None = None,
) -> tuple[
    ScheduleDraftService,
    SchedulePlanApplicationService,
    MySQLScheduleDraftRepository,
    MySQLScheduleApplicationRepository,
    ModelGateway,
    CalendarReadGateway,
]:
    """Compose durable Schedule review and atomic application services."""

    drafts = MySQLScheduleDraftRepository(sessions)
    applications = MySQLScheduleApplicationRepository(sessions)
    gateway = build_model_gateway(settings, observability=observability)
    prompts = PromptRegistry()
    prompts.register(SCHEDULE_AGENT_PROMPT_V1)
    agent = ScheduleAgent(
        gateway=gateway,
        prompts=prompts,
        request_timeout_seconds=settings.model_request_timeout_seconds,
        max_response_bytes=settings.model_max_response_bytes,
    )
    calendar_provider: CalendarReadProvider | None = None
    if settings.calendar_read_provider == "scripted":
        calendar_provider = ScriptedCalendarReadProvider()
    elif settings.calendar_read_provider == "http":
        if (
            settings.calendar_read_base_url is None
            or settings.calendar_read_api_key is None
        ):
            raise ValueError("Calendar read HTTP configuration is incomplete")
        calendar_provider = HttpCalendarReadProvider(
            base_url=settings.calendar_read_base_url.get_secret_value(),
            api_key=settings.calendar_read_api_key.get_secret_value(),
            timeout_seconds=settings.calendar_read_timeout_seconds,
            max_response_bytes=settings.calendar_read_max_response_bytes,
        )
    calendar = CalendarReadGateway(
        provider=calendar_provider,
        enabled=settings.calendar_read_enabled,
        timeout_seconds=settings.calendar_read_timeout_seconds,
        max_attempts=settings.calendar_read_max_attempts,
        user_id=user.id,
    )
    clock = SystemClock()
    service = ScheduleDraftService(
        profiles=profiles,
        plans=plans,
        check_ins=check_ins,
        drafts=drafts,
        contexts=contexts,
        calendar=calendar,
        agent=agent,
        clock=clock,
        metrics=ScheduleMetrics(),
        enabled=(settings.model_gateway_enabled and settings.schedule_agent_enabled),
        builder=TimeSlotCandidateSetBuilder(
            granularity_minutes=settings.schedule_slot_granularity_minutes,
            buffer_minutes=settings.schedule_min_buffer_minutes,
            max_per_session=settings.schedule_max_candidates_per_session,
            max_total=settings.schedule_max_total_candidates,
        ),
        ttl_minutes=settings.schedule_draft_ttl_minutes,
    )
    application = SchedulePlanApplicationService(
        profiles=profiles,
        exercises=exercises,
        plans=plans,
        check_ins=check_ins,
        drafts=drafts,
        contexts=context_snapshots,
        applications=applications,
        calendar=calendar,
        safety=safety,
        clock=clock,
        busy_snapshot_max_age_seconds=(settings.schedule_busy_snapshot_max_age_seconds),
    )
    return service, application, drafts, applications, gateway, calendar


def get_business_container(request: Request) -> BusinessContainer:
    """Return the lifespan-owned container or reject the unimplemented backend."""

    container = getattr(request.app.state, "business_container", None)
    if not isinstance(container, BusinessContainer):
        raise UnsupportedPersistenceBackend(
            "The selected persistence backend has no Phase 1A business repository."
        )
    return container


def get_tool_gateway(request: Request) -> ToolGateway:
    """Expose the lifespan-owned gateway to read-only operational endpoints."""

    gateway = getattr(request.app.state, "ics_tool_gateway", None)
    if isinstance(gateway, ToolGateway):
        return gateway
    return get_business_container(request).tool_gateway


def get_current_user(request: Request) -> UserAccount:
    """Return the configured local identity; this is not authentication."""

    user = getattr(request.app.state, "local_user", None)
    if isinstance(user, UserAccount):
        return user
    container = getattr(request.app.state, "business_container", None)
    if isinstance(container, BusinessContainer):
        return container.development_user
    raise UnsupportedPersistenceBackend("The configured local user is unavailable.")


def get_profile_repository(
    container: Annotated[BusinessContainer, Depends(get_business_container)],
) -> ProfileRepository:
    return container.profile_repository


def get_exercise_repository(
    container: Annotated[BusinessContainer, Depends(get_business_container)],
) -> ExerciseRepository:
    return container.exercise_repository


def get_plan_repository(
    container: Annotated[BusinessContainer, Depends(get_business_container)],
) -> PlanRepository:
    return container.plan_repository


def get_safety_engine(
    container: Annotated[BusinessContainer, Depends(get_business_container)],
) -> SafetyEngine:
    return container.safety_engine


def get_profile_service(request: Request) -> ProfileService:
    """Return the durable MySQL service when that backend owns the lifespan."""

    service = getattr(request.app.state, "profile_service", None)
    if isinstance(service, ProfileService):
        return service
    return get_business_container(request).profile_service


def get_exercise_service(request: Request) -> ExerciseCatalogService:
    service = getattr(request.app.state, "exercise_service", None)
    if isinstance(service, ExerciseCatalogService):
        return service
    return get_business_container(request).exercise_service


def get_plan_service(request: Request) -> PlanService:
    service = getattr(request.app.state, "plan_service", None)
    if isinstance(service, PlanService):
        return service
    return get_business_container(request).plan_service


def get_plan_generation_service(
    request: Request,
) -> PlanGenerationService:
    service = getattr(request.app.state, "plan_generation_service", None)
    if isinstance(service, PlanGenerationService):
        return service
    return get_business_container(request).plan_generation_service


def get_session_service(
    request: Request,
) -> SessionService:
    service = getattr(request.app.state, "session_service", None)
    if isinstance(service, SessionService):
        return service
    return get_business_container(request).session_service


def get_session_design_service(request: Request) -> SessionDesignService:
    service = getattr(request.app.state, "session_design_service", None)
    if isinstance(service, SessionDesignService):
        return service
    return get_business_container(request).session_design_service


def get_schedule_draft_service(request: Request) -> ScheduleDraftService:
    service = getattr(request.app.state, "schedule_draft_service", None)
    if isinstance(service, ScheduleDraftService):
        return service
    return get_business_container(request).schedule_draft_service


def get_schedule_plan_application_service(
    request: Request,
) -> SchedulePlanApplicationService:
    service = getattr(
        request.app.state,
        "schedule_plan_application_service",
        None,
    )
    if isinstance(service, SchedulePlanApplicationService):
        return service
    return get_business_container(request).schedule_plan_application_service


def get_ics_export_service(request: Request) -> IcsExportService:
    service = getattr(request.app.state, "ics_export_service", None)
    if isinstance(service, IcsExportService):
        return service
    return get_business_container(request).ics_export_service


def get_calendar_operation_service(request: Request) -> CalendarOperationService:
    """Resolve the durable Calendar review service before memory fallback."""

    service = getattr(request.app.state, "calendar_operation_service", None)
    if isinstance(service, CalendarOperationService):
        return service
    return get_business_container(request).calendar_operation_service


def get_schedule_application_run_service(
    request: Request,
) -> ScheduleApplicationRunService:
    """Resolve the durable MySQL Schedule Run service before memory fallback."""

    service = getattr(request.app.state, "schedule_application_run_service", None)
    if isinstance(service, ScheduleApplicationRunService):
        return service
    return get_business_container(request).schedule_application_run_service


def get_calendar_operation_run_service(request: Request) -> CalendarOperationRunService:
    service = getattr(request.app.state, "calendar_operation_run_service", None)
    if isinstance(service, CalendarOperationRunService):
        return service
    return get_business_container(request).calendar_operation_run_service


def get_session_design_plan_application_service(
    request: Request,
) -> SessionDesignPlanApplicationService:
    service = getattr(
        request.app.state,
        "session_design_plan_application_service",
        None,
    )
    if isinstance(service, SessionDesignPlanApplicationService):
        return service
    return get_business_container(request).session_design_plan_application_service


def get_session_design_application_run_service(
    request: Request,
) -> SessionDesignApplicationRunService:
    """Resolve the durable MySQL Session Run service before memory fallback."""

    service = getattr(request.app.state, "session_design_application_run_service", None)
    if isinstance(service, SessionDesignApplicationRunService):
        return service
    return get_business_container(request).session_design_application_run_service


def get_check_in_service(request: Request) -> CheckInService:
    service = getattr(request.app.state, "check_in_service", None)
    if isinstance(service, CheckInService):
        return service
    return get_business_container(request).check_in_service


def get_progress_service(request: Request) -> ProgressService:
    service = getattr(request.app.state, "progress_service", None)
    if isinstance(service, ProgressService):
        return service
    return get_business_container(request).progress_service


def get_local_replanning_service(
    request: Request,
) -> LocalReplanningService:
    service = getattr(request.app.state, "local_replanning_service", None)
    if isinstance(service, LocalReplanningService):
        return service
    return get_business_container(request).local_replanning_service


def get_orchestration_service(request: Request) -> OrchestrationService:
    """Resolve durable MySQL state before the explicit memory fallback."""

    service = getattr(request.app.state, "orchestration_service", None)
    if isinstance(service, OrchestrationService):
        return service
    return get_business_container(request).orchestration_service


def get_profile_agent_service(request: Request) -> ProfileAgentService:
    """Resolve the lifespan-owned MySQL Context Profile Agent when configured."""

    service = getattr(request.app.state, "profile_agent_service", None)
    if isinstance(service, ProfileAgentService):
        return service
    return get_business_container(request).profile_agent_service


def get_profile_draft_preview_service(
    request: Request,
) -> ProfileDraftPreviewService:
    service = getattr(request.app.state, "profile_draft_preview_service", None)
    if isinstance(service, ProfileDraftPreviewService):
        return service
    return get_business_container(request).profile_draft_preview_service


def get_profile_draft_apply_service(
    request: Request,
) -> ProfileDraftApplyService:
    service = getattr(request.app.state, "profile_draft_apply_service", None)
    if isinstance(service, ProfileDraftApplyService):
        return service
    return get_business_container(request).profile_draft_apply_service


def get_profile_draft_memory_candidate_service(
    container: Annotated[BusinessContainer, Depends(get_business_container)],
) -> ProfileDraftMemoryCandidateService:
    return container.profile_draft_memory_candidate_service


def get_profile_agent_run_service(request: Request) -> ProfileAgentRunService:
    """Resolve the durable MySQL Profile Run service before memory fallback."""

    service = getattr(request.app.state, "profile_agent_run_service", None)
    if isinstance(service, ProfileAgentRunService):
        return service
    return get_business_container(request).profile_agent_run_service


def get_memory_application_service(request: Request) -> MemoryApplicationService:
    """Resolve the lifespan-owned durable service before memory-mode fallback."""

    service = getattr(request.app.state, "memory_application_service", None)
    if isinstance(service, MemoryApplicationService):
        return service
    return get_business_container(request).memory_application_service


def get_context_application_service(request: Request) -> ContextApplicationService:
    service = getattr(request.app.state, "context_application_service", None)
    if isinstance(service, ContextApplicationService):
        return service
    return get_business_container(request).context_application_service


def get_recovery_draft_service(request: Request) -> RecoveryDraftService:
    """Resolve durable MySQL Recovery state before explicit memory mode."""

    service = getattr(request.app.state, "recovery_draft_service", None)
    if isinstance(service, RecoveryDraftService):
        return service
    return get_business_container(request).recovery_draft_service


def get_recovery_application_service(request: Request) -> RecoveryApplicationService:
    """Resolve MySQL Recovery Application before explicit memory-mode fallback."""

    service = getattr(request.app.state, "recovery_application_service", None)
    if isinstance(service, RecoveryApplicationService):
        return service
    return get_business_container(request).recovery_application_service


def get_recovery_application_run_service(
    request: Request,
) -> RecoveryApplicationRunService:
    """Resolve MySQL Recovery Run control-plane before memory-mode fallback."""

    service = getattr(request.app.state, "recovery_application_run_service", None)
    if isinstance(service, RecoveryApplicationRunService):
        return service
    return get_business_container(request).recovery_application_run_service
