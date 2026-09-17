"""One bounded OpenAI-compatible chat-completions HTTP provider."""

import json
import time
from typing import cast

import httpx

from app.domain.model_gateway.errors import (
    ModelConnectionError,
    ModelEmptyResponseError,
    ModelGatewayError,
    ModelInvalidRequestError,
    ModelResponseTooLargeError,
    ModelTimeoutError,
    error_for_http_status,
)
from app.domain.model_gateway.models import ModelProviderResponse, ModelRequest


class OpenAICompatibleHttpProvider:
    """HTTP-only provider; constructing it does not perform network I/O."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        provider_name: str = "http",
        provider_version: str,
        connect_timeout_seconds: float,
        max_response_bytes: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.provider_version = provider_version
        self._endpoint = self._build_endpoint(base_url)
        self._api_key = api_key
        self._connect_timeout_seconds = connect_timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(provider_name={self.provider_name!r}, "
            "api_key='**********')"
        )

    @staticmethod
    def _build_endpoint(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if normalized.endswith("/chat/completions"):
            return normalized
        return f"{normalized}/chat/completions"

    async def invoke(self, request: ModelRequest) -> ModelProviderResponse:
        if self._closed:
            raise ModelConnectionError("The model HTTP client is closed.")
        timeout = httpx.Timeout(
            timeout=request.timeout_seconds,
            connect=min(self._connect_timeout_seconds, request.timeout_seconds),
            read=request.timeout_seconds,
            write=request.timeout_seconds,
            pool=request.timeout_seconds,
        )
        payload = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        started = time.perf_counter()
        try:
            async with self._client.stream(
                "POST",
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise error_for_http_status(response.status_code)
                content_type = response.headers.get("content-type", "").casefold()
                if "application/json" not in content_type:
                    raise ModelInvalidRequestError(
                        "The model provider returned an unsupported content type."
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        announced_size = int(content_length)
                    except ValueError as exc:
                        raise ModelInvalidRequestError(
                            "The model provider returned an invalid content length."
                        ) from exc
                    if announced_size > self._max_response_bytes:
                        raise ModelResponseTooLargeError()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._max_response_bytes:
                        raise ModelResponseTooLargeError()
                    chunks.append(chunk)
        except ModelGatewayError:
            raise
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError() from exc
        except httpx.RequestError as exc:
            raise ModelConnectionError() from exc
        raw_bytes = b"".join(chunks)
        if not raw_bytes:
            raise ModelEmptyResponseError()
        try:
            document = json.loads(raw_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ModelInvalidRequestError(
                "The model provider returned malformed JSON."
            ) from exc
        if not isinstance(document, dict):
            raise ModelInvalidRequestError(
                "The model provider response is not an object."
            )
        choices = document.get("choices")
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], dict)
        ):
            raise ModelInvalidRequestError("The model provider response has no choice.")
        first = cast(dict[str, object], choices[0])
        message = first.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ModelInvalidRequestError("The model provider response has no text.")
        usage = document.get("usage")
        input_tokens: int | None = None
        output_tokens: int | None = None
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            input_tokens = prompt_tokens if isinstance(prompt_tokens, int) else None
            output_tokens = (
                completion_tokens if isinstance(completion_tokens, int) else None
            )
        provider_request_id = document.get("id")
        return ModelProviderResponse(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            model=request.model,
            raw_text=cast(str, message["content"]),
            finish_reason=(
                cast(str, first["finish_reason"])
                if isinstance(first.get("finish_reason"), str)
                else None
            ),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider_request_id=(
                provider_request_id if isinstance(provider_request_id, str) else None
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()
