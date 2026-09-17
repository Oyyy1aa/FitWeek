"""Static permission matrix, deliberately separate from model-selected tools."""

from app.domain.tools.enums import ToolCaller, ToolId

TOOL_PERMISSION_MATRIX: dict[ToolId, frozenset[ToolCaller]] = {
    ToolId.EXERCISE_CATALOG_SEARCH: frozenset(
        {
            ToolCaller.PLAN_GENERATION_APPLICATION,
            ToolCaller.SESSION_DESIGN_APPLICATION,
            ToolCaller.RECOVERY_APPLICATION,
        }
    ),
    ToolId.SESSION_DURATION_CALCULATOR: frozenset(
        {
            ToolCaller.SESSION_DESIGN_APPLICATION,
            ToolCaller.RECOVERY_APPLICATION,
        }
    ),
    ToolId.CALENDAR_FREE_BUSY: frozenset(
        {
            ToolCaller.SCHEDULE_APPLICATION,
            ToolCaller.RECOVERY_APPLICATION,
        }
    ),
    ToolId.RECOVERY_SPACING_VALIDATOR: frozenset({ToolCaller.RECOVERY_APPLICATION}),
    ToolId.ICS_EXPORT: frozenset({ToolCaller.ICS_EXPORT_SERVICE}),
    ToolId.CALENDAR_COMMIT: frozenset({ToolCaller.CALENDAR_EXECUTOR}),
    ToolId.MEMORY_CANDIDATE_CREATE: frozenset({ToolCaller.MEMORY_COMMITTER}),
}
