"""Persistence-neutral Profile Draft review conflicts."""


class ProfileDraftReviewError(RuntimeError):
    """Base failure raised by the atomic review repository port."""


class ProfileDraftNotFoundError(ProfileDraftReviewError):
    pass


class ProfileDraftExpiredError(ProfileDraftReviewError):
    pass


class ProfileDraftAlreadyAppliedError(ProfileDraftReviewError):
    pass


class ProfileDraftRejectedError(ProfileDraftReviewError):
    pass


class ProfileDraftVersionConflictError(ProfileDraftReviewError):
    pass


class ProfileVersionConflictError(ProfileDraftReviewError):
    pass


class ProfileStateConflictError(ProfileDraftReviewError):
    pass


class ProfileDraftApplyIdempotencyConflictError(ProfileDraftReviewError):
    pass


class ProfileDraftRejectIdempotencyConflictError(ProfileDraftReviewError):
    pass
