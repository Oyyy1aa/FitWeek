"""Top-level API router."""

from fastapi import APIRouter

from app.api.calendar_operation_runs import router as calendar_operation_runs_router
from app.api.calendar_operations import router as calendar_operations_router
from app.api.contexts import router as contexts_router
from app.api.contexts import snapshots_router
from app.api.exercises import router as exercises_router
from app.api.health import router as health_router
from app.api.ics_exports import router as ics_exports_router
from app.api.memories import router as memories_router
from app.api.planning_runs import router as planning_runs_router
from app.api.plans import router as plans_router
from app.api.profile_agent import router as profile_agent_router
from app.api.profile_agent_runs import router as profile_agent_runs_router
from app.api.profiles import router as profiles_router
from app.api.recovery_application_runs import (
    router as recovery_application_runs_router,
)
from app.api.recovery_drafts import router as recovery_drafts_router
from app.api.schedule_application_runs import router as schedule_application_runs_router
from app.api.schedule_applications import router as schedule_applications_router
from app.api.schedules import router as schedules_router
from app.api.session_design_application_runs import (
    router as session_design_application_runs_router,
)
from app.api.session_design_applications import (
    router as session_design_applications_router,
)
from app.api.session_designs import router as session_designs_router
from app.api.sessions import router as sessions_router
from app.api.tool_gateway import router as tool_gateway_router
from app.api.users import router as users_router

router = APIRouter()
router.include_router(health_router)

v1_router = APIRouter(prefix="/api/v1")
v1_router.include_router(users_router)
v1_router.include_router(profiles_router)
v1_router.include_router(exercises_router)
v1_router.include_router(plans_router)
v1_router.include_router(sessions_router)
v1_router.include_router(tool_gateway_router)
v1_router.include_router(schedules_router)
v1_router.include_router(schedule_applications_router)
v1_router.include_router(ics_exports_router)
v1_router.include_router(calendar_operations_router)
v1_router.include_router(schedule_application_runs_router)
v1_router.include_router(calendar_operation_runs_router)
v1_router.include_router(session_designs_router)
v1_router.include_router(session_design_applications_router)
v1_router.include_router(session_design_application_runs_router)
v1_router.include_router(planning_runs_router)
v1_router.include_router(profile_agent_router)
v1_router.include_router(profile_agent_runs_router)
v1_router.include_router(recovery_drafts_router)
v1_router.include_router(recovery_application_runs_router)
v1_router.include_router(memories_router)
v1_router.include_router(contexts_router)
v1_router.include_router(snapshots_router)
router.include_router(v1_router)
