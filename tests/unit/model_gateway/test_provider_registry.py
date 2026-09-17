"""Provider registry is explicit and closes registered providers once."""

import pytest

from app.domain.model_gateway.errors import ModelProviderUnavailableError
from app.model_gateway.fake_provider import ScriptedFakeProvider
from app.model_gateway.provider_registry import ProviderRegistry

pytestmark = pytest.mark.phase_3a


@pytest.mark.asyncio
async def test_register_get_duplicate_missing_and_close() -> None:
    registry = ProviderRegistry()
    provider = ScriptedFakeProvider()
    registry.register("scripted-fake", provider)

    assert registry.get("SCRIPTED-FAKE") is provider
    with pytest.raises(ValueError):
        registry.register("scripted-fake", provider)
    with pytest.raises(ModelProviderUnavailableError):
        registry.get("missing")

    await registry.close()
    assert provider.closed is True
