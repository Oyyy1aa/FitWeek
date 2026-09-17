"""In-memory persistence adapters for the phase 1A development mode."""

from app.persistence.memory.calendar_operation_repository import (
    InMemoryCalendarOperationRepository,
)
from app.persistence.memory.checkin_repository import InMemoryCheckInRepository
from app.persistence.memory.context_snapshot_repository import (
    InMemoryContextSnapshotRepository,
)
from app.persistence.memory.draft_memory_candidate_repository import (
    InMemoryDraftMemoryCandidateImportRepository,
)
from app.persistence.memory.exercise_repository import InMemoryExerciseRepository
from app.persistence.memory.ics_export_repository import InMemoryIcsExportRepository
from app.persistence.memory.memory_repository import InMemoryMemoryRepository
from app.persistence.memory.orchestration_repository import (
    InMemoryOrchestrationRepository,
)
from app.persistence.memory.plan_repository import InMemoryPlanRepository
from app.persistence.memory.profile_agent_repository import (
    InMemoryProfileAgentDraftRepository,
)
from app.persistence.memory.profile_draft_review_repository import (
    InMemoryProfileDraftReviewRepository,
)
from app.persistence.memory.profile_repository import InMemoryProfileRepository
from app.persistence.memory.recovery_application_repository import (
    InMemoryRecoveryApplicationRepository,
)
from app.persistence.memory.recovery_draft_repository import (
    InMemoryRecoveryDraftRepository,
)
from app.persistence.memory.schedule_application_repository import (
    InMemoryScheduleApplicationRepository,
)
from app.persistence.memory.schedule_repository import InMemoryScheduleDraftRepository
from app.persistence.memory.session_design_application_repository import (
    InMemorySessionDesignApplicationRepository,
)
from app.persistence.memory.session_design_repository import (
    InMemorySessionDesignRepository,
)
from app.persistence.memory.store import (
    InMemoryStore,
    RepositoryConflictError,
    RepositoryError,
    RepositoryUniqueError,
)

__all__ = [
    "InMemoryCheckInRepository",
    "InMemoryCalendarOperationRepository",
    "InMemoryContextSnapshotRepository",
    "InMemoryDraftMemoryCandidateImportRepository",
    "InMemoryExerciseRepository",
    "InMemoryMemoryRepository",
    "InMemoryOrchestrationRepository",
    "InMemoryPlanRepository",
    "InMemoryProfileRepository",
    "InMemoryRecoveryDraftRepository",
    "InMemoryRecoveryApplicationRepository",
    "InMemorySessionDesignRepository",
    "InMemoryScheduleDraftRepository",
    "InMemoryScheduleApplicationRepository",
    "InMemoryIcsExportRepository",
    "InMemorySessionDesignApplicationRepository",
    "InMemoryProfileAgentDraftRepository",
    "InMemoryProfileDraftReviewRepository",
    "InMemoryStore",
    "RepositoryConflictError",
    "RepositoryError",
    "RepositoryUniqueError",
]
