"""Non-successful upstream outcomes are mapped to explicit safe modes."""

from app.domain.tools.enums import ToolDegradationMode, ToolId


def degradation_for(tool_id: ToolId) -> ToolDegradationMode:
    return {
        ToolId.CALENDAR_FREE_BUSY: ToolDegradationMode.MANUAL_ONLY,
        ToolId.CALENDAR_COMMIT: ToolDegradationMode.EXTERNAL_WRITE_DISABLED,
        ToolId.MEMORY_CANDIDATE_CREATE: ToolDegradationMode.NO_MEMORY,
    }.get(tool_id, ToolDegradationMode.NONE)
