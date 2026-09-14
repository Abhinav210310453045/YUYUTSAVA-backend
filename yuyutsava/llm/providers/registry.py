"""Which provider builds the chat model for a given settings object.

Dispatch is by settings type: each provider declares the settings class it handles,
and anything else — Groq, OpenRouter, Ollama, OpenAI and other OpenAI-compatible
hosts — falls back to the OpenAI-compatible provider. Settings classes are unrelated
dataclasses, so the order of ``_PROVIDERS`` carries no meaning.
"""

from __future__ import annotations

from yuyutsava.core.config import LlmSettings
from yuyutsava.llm.base import Provider
from yuyutsava.llm.providers.anthropic import AnthropicProvider
from yuyutsava.llm.providers.azure import AzureOpenAIProvider
from yuyutsava.llm.providers.bedrock import BedrockProvider
from yuyutsava.llm.providers.cohere import CohereProvider
from yuyutsava.llm.providers.google import GoogleProvider
from yuyutsava.llm.providers.mistral import MistralProvider
from yuyutsava.llm.providers.openai_compat import OpenAICompatibleProvider
from yuyutsava.llm.providers.vertex import VertexProvider

# Providers selected by settings type. Instantiated once — they are stateless.
_PROVIDERS: tuple[Provider, ...] = (
    AnthropicProvider(),
    GoogleProvider(),
    VertexProvider(),
    BedrockProvider(),
    AzureOpenAIProvider(),
    MistralProvider(),
    CohereProvider(),
)

# Serves every OpenAI-compatible host (Groq, OpenRouter, Ollama, OpenAI, …),
# which is why it is matched by exclusion rather than by type.
_FALLBACK: Provider = OpenAICompatibleProvider()


def provider_for(settings: LlmSettings) -> Provider:
    """The provider that builds *settings*' chat model."""
    for provider in _PROVIDERS:
        if provider.settings_type is not None and isinstance(settings, provider.settings_type):
            return provider
    return _FALLBACK
