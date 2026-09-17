"""Explicit provider registration; no dynamic discovery or network loading."""

from app.domain.model_gateway.errors import ModelProviderUnavailableError
from app.domain.model_gateway.protocols import ModelProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}

    def register(self, name: str, provider: ModelProvider) -> None:
        normalized = name.strip().lower()
        if not normalized or normalized in self._providers:
            raise ValueError(f"provider {normalized!r} is already registered")
        self._providers[normalized] = provider

    def get(self, name: str) -> ModelProvider:
        try:
            return self._providers[name.strip().lower()]
        except KeyError as exc:
            raise ModelProviderUnavailableError(
                "The configured model provider is not registered."
            ) from exc

    async def close(self) -> None:
        for provider in dict.fromkeys(self._providers.values()):
            await provider.close()
