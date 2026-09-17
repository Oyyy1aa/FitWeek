"""Actual loopback HTTP verification against the ephemeral stub server."""

import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
import pytest

from app.domain.model_gateway.errors import (
    ModelAuthenticationError,
    ModelRateLimitedError,
    ModelResponseTooLargeError,
    ModelServerError,
    ModelTimeoutError,
)
from app.model_gateway.http_provider import OpenAICompatibleHttpProvider
from tests.unit.model_gateway.test_http_provider import model_request

pytestmark = pytest.mark.phase_3a


def free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


@pytest.fixture(scope="module")
def stub_base_url() -> Iterator[str]:
    port = free_port()
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "tests.stub_model_server.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/openapi.json", timeout=0.2)
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.03)
    else:
        process.terminate()
        pytest.fail("local model stub did not start")
    try:
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.asyncio
async def test_actual_stub_success_and_markdown_json(stub_base_url: str) -> None:
    for scenario in ("success", "markdown-json", "invalid-json"):
        provider = OpenAICompatibleHttpProvider(
            base_url=f"{stub_base_url}/{scenario}",
            api_key="local-test-key",
            provider_version="stub-v1",
            connect_timeout_seconds=0.2,
            max_response_bytes=262144,
        )
        response = await provider.invoke(model_request())
        await provider.close()
        assert response.raw_text


@pytest.mark.parametrize(
    ("scenario", "error_type"),
    [
        ("rate-limited", ModelRateLimitedError),
        ("server-error", ModelServerError),
        ("auth-error", ModelAuthenticationError),
        ("timeout", ModelTimeoutError),
        ("oversized", ModelResponseTooLargeError),
    ],
)
@pytest.mark.asyncio
async def test_actual_stub_faults_are_bounded_and_classified(
    stub_base_url: str,
    scenario: str,
    error_type: type[Exception],
) -> None:
    provider = OpenAICompatibleHttpProvider(
        base_url=f"{stub_base_url}/{scenario}",
        api_key="local-test-key",
        provider_version="stub-v1",
        connect_timeout_seconds=0.05,
        max_response_bytes=1024,
    )
    with pytest.raises(error_type):
        await provider.invoke(model_request(timeout=0.05))
    await provider.close()
