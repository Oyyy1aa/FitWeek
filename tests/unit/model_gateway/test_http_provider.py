"""OpenAI-compatible HTTP contract normalization tests."""

import asyncio
from uuid import uuid4

import httpx
import pytest

from app.domain.model_gateway.errors import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelEmptyResponseError,
    ModelGatewayError,
    ModelInvalidRequestError,
    ModelPermissionError,
    ModelRateLimitedError,
    ModelResponseTooLargeError,
    ModelServerError,
    ModelTimeoutError,
)
from app.domain.model_gateway.models import ModelRequest
from app.model_gateway.http_provider import OpenAICompatibleHttpProvider

pytestmark = pytest.mark.phase_3a


def model_request(*, timeout: float = 1) -> ModelRequest:
    return ModelRequest(
        request_id=uuid4(),
        model="stub-model",
        system_prompt="system",
        user_prompt="user",
        response_schema_name="test",
        temperature=0,
        max_output_tokens=100,
        timeout_seconds=timeout,
        metadata={"agent": "test"},
    )


def provider_with_transport(
    handler: httpx.AsyncBaseTransport,
    *,
    max_bytes: int = 4096,
) -> tuple[OpenAICompatibleHttpProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=handler)
    provider = OpenAICompatibleHttpProvider(
        base_url="https://stub.invalid",
        api_key="secret-not-for-output",
        provider_version="test-v1",
        connect_timeout_seconds=0.1,
        max_response_bytes=max_bytes,
        client=client,
    )
    return provider, client


@pytest.mark.asyncio
async def test_success_normalizes_fields_without_inventing_tokens() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-not-for-output"
        return httpx.Response(
            200,
            json={
                "id": "provider-request",
                "choices": [
                    {
                        "message": {"content": '{"ok":true}'},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    provider, client = provider_with_transport(httpx.MockTransport(handler))
    response = await provider.invoke(model_request())
    await provider.close()
    await client.aclose()

    assert response.raw_text == '{"ok":true}'
    assert response.input_tokens is None
    assert response.output_tokens is None
    assert "secret-not-for-output" not in repr(provider)


@pytest.mark.parametrize(
    ("status_code", "error_type", "retryable"),
    [
        (400, ModelInvalidRequestError, False),
        (401, ModelAuthenticationError, False),
        (403, ModelPermissionError, False),
        (429, ModelRateLimitedError, True),
        (500, ModelServerError, True),
        (502, ModelServerError, True),
        (503, ModelServerError, True),
        (504, ModelServerError, True),
    ],
)
@pytest.mark.asyncio
async def test_http_statuses_are_classified(
    status_code: int,
    error_type: type[ModelGatewayError],
    retryable: bool,
) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(status_code, json={"error": "not exposed"})
    )
    provider, client = provider_with_transport(transport)
    with pytest.raises(error_type) as raised:
        await provider.invoke(model_request())
    await client.aclose()

    assert raised.value.retryable is retryable


@pytest.mark.asyncio
async def test_content_type_shape_empty_and_size_are_rejected() -> None:
    responses = iter(
        (
            httpx.Response(200, text="plain", headers={"content-type": "text/plain"}),
            httpx.Response(200, json={"choices": []}),
            httpx.Response(
                200, content=b"", headers={"content-type": "application/json"}
            ),
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": "x" * 5000}}]},
            ),
        )
    )
    provider, client = provider_with_transport(
        httpx.MockTransport(lambda _: next(responses)),
        max_bytes=1024,
    )

    with pytest.raises(ModelInvalidRequestError):
        await provider.invoke(model_request())
    with pytest.raises(ModelInvalidRequestError):
        await provider.invoke(model_request())
    with pytest.raises(ModelEmptyResponseError):
        await provider.invoke(model_request())
    with pytest.raises(ModelResponseTooLargeError):
        await provider.invoke(model_request())
    await client.aclose()


@pytest.mark.asyncio
async def test_timeout_connection_close_and_cancellation_are_preserved() -> None:
    async def timeout_handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    timeout_provider, timeout_client = provider_with_transport(
        httpx.MockTransport(timeout_handler)
    )
    with pytest.raises(ModelTimeoutError):
        await timeout_provider.invoke(model_request())
    await timeout_client.aclose()

    async def connection_handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection")

    connection_provider, connection_client = provider_with_transport(
        httpx.MockTransport(connection_handler)
    )
    with pytest.raises(ModelConnectionError):
        await connection_provider.invoke(model_request())
    await connection_provider.close()
    with pytest.raises(ModelConnectionError, match="closed"):
        await connection_provider.invoke(model_request())
    await connection_client.aclose()

    async def cancelled_handler(_: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    cancelled_provider, cancelled_client = provider_with_transport(
        httpx.MockTransport(cancelled_handler)
    )
    with pytest.raises(asyncio.CancelledError):
        await cancelled_provider.invoke(model_request())
    await cancelled_client.aclose()
