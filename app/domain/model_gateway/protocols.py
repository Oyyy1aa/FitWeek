"""Ports implemented by concrete model providers."""

from typing import Protocol

from app.domain.model_gateway.models import ModelProviderResponse, ModelRequest


class ModelProvider(Protocol):
    provider_name: str
    provider_version: str

    async def invoke(self, request: ModelRequest) -> ModelProviderResponse: ...

    async def close(self) -> None: ...
