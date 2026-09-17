"""Explicit scripted provider for local contract tests and fault injection."""

import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.model_gateway.errors import ModelGatewayError
from app.domain.model_gateway.models import ModelProviderResponse, ModelRequest
from app.model_gateway.redaction import summarize_request

FakeAction = str | ModelGatewayError | ModelProviderResponse


@dataclass(frozen=True, slots=True)
class FakeRequestSummary:
    values: dict[str, object]


class ScriptedFakeProvider:
    """Return caller-supplied actions in order without interpreting prompts."""

    def __init__(
        self,
        *,
        provider_name: str = "scripted-fake",
        provider_version: str = "phase-3a-v1",
        script: Iterable[FakeAction] = (),
        default_raw_text: str = "{}",
    ) -> None:
        self.provider_name = provider_name
        self.provider_version = provider_version
        self._script = deque(script)
        self._default_raw_text = default_raw_text
        self._request_summaries: list[FakeRequestSummary] = []
        self.call_count = 0
        self.closed = False

    @property
    def request_summaries(self) -> tuple[FakeRequestSummary, ...]:
        return tuple(self._request_summaries)

    async def invoke(self, request: ModelRequest) -> ModelProviderResponse:
        if self.closed:
            raise RuntimeError("scripted fake provider is closed")
        self.call_count += 1
        self._request_summaries.append(FakeRequestSummary(summarize_request(request)))
        action = self._script.popleft() if self._script else self._default_raw_text
        if isinstance(action, ModelGatewayError):
            raise action
        if isinstance(action, ModelProviderResponse):
            return action
        started = time.perf_counter()
        return ModelProviderResponse(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            model=request.model,
            raw_text=action,
            finish_reason="stop",
            input_tokens=None,
            output_tokens=None,
            provider_request_id=None,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def close(self) -> None:
        self.closed = True
