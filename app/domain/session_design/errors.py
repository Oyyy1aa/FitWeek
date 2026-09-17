"""Stable Session Designer domain error codes."""


class SessionDesignValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)
