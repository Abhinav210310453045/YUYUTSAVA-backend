"""Which provider builds the chat model for a given target.

Dispatch is by the target's connection ``type``: each provider declares the
types it serves (:attr:`~yuyutsava.llm.base.Provider.types`), and this table is
built from those declarations, so adding a provider is one entry in
``_PROVIDERS``. ``test/llm/test_model_handle.py`` checks that every type the
config schema accepts has exactly one provider.
"""

from __future__ import annotations

from yuyutsava.llm.base import Provider
from yuyutsava.llm.providers.anthropic import AnthropicProvider
from yuyutsava.llm.providers.azure import AzureOpenAIProvider
from yuyutsava.llm.providers.bedrock import BedrockProvider
from yuyutsava.llm.providers.cohere import CohereProvider
from yuyutsava.llm.providers.google import GoogleProvider
from yuyutsava.llm.providers.mistral import MistralProvider
from yuyutsava.llm.providers.openai_compat import OpenAICompatibleProvider
from yuyutsava.llm.providers.vertex import VertexProvider
from yuyutsava.llm.target import ModelTarget

# Instantiated once — providers are stateless.
_PROVIDERS: tuple[Provider, ...] = (
    VertexProvider(),
    GoogleProvider(),
    BedrockProvider(),
    AnthropicProvider(),
    OpenAICompatibleProvider(),
    MistralProvider(),
    CohereProvider(),
    AzureOpenAIProvider(),
)

_BY_TYPE: dict[str, Provider] = {
    kind: provider for provider in _PROVIDERS for kind in provider.types
}


def provider_for(target: ModelTarget) -> Provider:
    """The provider that builds *target*'s chat model."""
    return _BY_TYPE[target.type]


def all_providers() -> tuple[Provider, ...]:
    """Every provider — for checks that must cover each one."""
    return _PROVIDERS


__all__ = ["all_providers", "provider_for"]
