"""Non-sensitive classified failures crossing Tool Gateway boundaries."""

from app.domain.tools.enums import ToolErrorCategory


class ToolError(RuntimeError):
    def __init__(self, category: ToolErrorCategory, code: str) -> None:
        super().__init__(code)
        self.category = category
        self.code = code


class ToolPermissionDenied(ToolError):
    def __init__(self) -> None:
        super().__init__(ToolErrorCategory.PERMISSION, "TOOL_PERMISSION_DENIED")
