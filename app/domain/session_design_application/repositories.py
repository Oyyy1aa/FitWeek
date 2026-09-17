"""Persistence port for atomic in-memory Session Design application commits."""

from typing import Protocol
from uuid import UUID

from app.domain.plans.models import WeeklyPlan
from app.domain.session_design.models import SessionDesignDraft
from app.domain.session_design_application.models import (
    SessionDesignApplicationCommit,
    SessionDesignApplicationResult,
)


class SessionDesignApplicationRepository(Protocol):
    async def get_result(
        self, user_id: UUID, result_id: UUID
    ) -> SessionDesignApplicationResult | None: ...

    async def get_result_by_request(
        self, user_id: UUID, client_request_id: str
    ) -> SessionDesignApplicationResult | None: ...

    async def get_result_by_draft(
        self, user_id: UUID, draft_id: UUID
    ) -> SessionDesignApplicationResult | None: ...

    async def commit(
        self,
        *,
        source: WeeklyPlan,
        expected_draft: SessionDesignDraft,
        applied_draft: SessionDesignDraft,
        revision: WeeklyPlan,
        result: SessionDesignApplicationResult,
    ) -> SessionDesignApplicationCommit: ...

    async def clear(self) -> None: ...
