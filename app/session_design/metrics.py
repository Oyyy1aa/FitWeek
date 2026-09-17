"""Process-local Session Designer contract metrics."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class SessionDesignMetricsSnapshot:
    requests_total: int
    model_drafts: int
    template_fallbacks: int
    validation_failures: int
    idempotent_reuses: int
    accepted: int
    rejected: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class SessionDesignMetrics:
    def __init__(self) -> None:
        self.requests_total = 0
        self.model_drafts = 0
        self.template_fallbacks = 0
        self.validation_failures = 0
        self.idempotent_reuses = 0
        self.accepted = 0
        self.rejected = 0

    def snapshot(self) -> SessionDesignMetricsSnapshot:
        return SessionDesignMetricsSnapshot(
            **{
                name: getattr(self, name)
                for name in SessionDesignMetricsSnapshot.__dataclass_fields__
            }
        )
