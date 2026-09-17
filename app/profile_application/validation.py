"""Internal deterministic merge validation failures."""

from app.domain.profile_agent.apply_models import ApplyValidationCode


class ProfileDraftMergeError(ValueError):
    def __init__(
        self,
        code: ApplyValidationCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code
