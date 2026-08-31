"""
yuyutsava.llm — the LLM provider layer.

Owns everything about talking to a model provider that is not pure config: which
optional SDK to import, how to construct its chat model, and which wire-format
quirks that SDK needs corrected.

    factory    chat_model() — the single seam every model is built through
    handle     ModelHandle — a model plus its identity and capabilities
    base       the Provider contract + require() for optional SDKs
    providers  one module per provider (anthropic, vertex, google, …)
    quirks     reusable per-provider wire fixes, shared across providers

Supported providers (set ``LLM_PROVIDER``):

OpenAI-compatible — all served by ``providers/openai_compat.py``, which differs
only by ``base_url``:
    groq · openrouter · ollama · openai · openai_compatible
        (xAI, DeepSeek, Together, Fireworks, Perplexity, …)

Native SDKs (lazy-imported; install the matching extra):
    anthropic → ChatAnthropic            (prompt caching via middleware)
    google    → ChatGoogleGenerativeAI   [yuyutsava[google]]   ⎫ Gemini wire format:
    vertex    → ChatVertexAI             [yuyutsava[vertex]]   ⎭ quirks/gemini_parts
    bedrock   → ChatBedrockConverse      [yuyutsava[bedrock]]
    azure     → AzureChatOpenAI          (langchain-openai, already installed)
    mistral   → ChatMistralAI            [yuyutsava[mistral]]
    cohere    → ChatCohere               [yuyutsava[cohere]]

Layering: this package imports ``yuyutsava.core.config`` (provider DATA — how to
reach a provider) and never the reverse. Config is a leaf that ~31 modules depend
on; a quirk hook hanging off a Settings class would invert that.
"""

from yuyutsava.llm.base import Provider, require
from yuyutsava.llm.factory import (
    chat_model,
    handle_for,
    model_handle,
    model_name_of,
    supports,
)
from yuyutsava.llm.handle import Capability, ModelHandle
from yuyutsava.llm.providers import provider_for

__all__ = [
    "Capability",
    "ModelHandle",
    "Provider",
    "chat_model",
    "handle_for",
    "model_handle",
    "model_name_of",
    "provider_for",
    "require",
    "supports",
]
