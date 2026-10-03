"""What is known about a model before it is called: for now, its context window.

The window is what the context budget steers under — when compaction triggers,
what the context meter shows. Today it is known per provider *type*: the window
of the family that type usually serves. Per-model windows (from the models
config, discovery and LangChain's model profiles) take its place when the context
budget follows the active model (design §7, M3).
"""

from __future__ import annotations

#: The window to assume for a provider type that is not listed.
FALLBACK_CONTEXT_WINDOW = 128_000

#: Input window per provider type, in tokens.
_CONTEXT_WINDOWS: dict[str, int] = {
    # Gemini takes ~1M, on Vertex and on AI Studio.
    "vertex": 1_000_000,
    "google": 1_000_000,
    # Claude takes 200k, from Anthropic and on Bedrock.
    "anthropic": 200_000,
    "bedrock": 200_000,
    # Local models: a small window unless the server is configured larger.
    "ollama": 8_192,
    # Groq's open models, OpenRouter's common window, the GPT-4o family,
    # Mistral Large, Command R: all 128k.
    "groq": 128_000,
    "openrouter": 128_000,
    "openai": 128_000,
    "openai_compatible": 128_000,
    "azure": 128_000,
    "mistral": 128_000,
    "cohere": 128_000,
}


def default_context_window(provider_type: str) -> int:
    """The input window to steer under for a model of *provider_type*."""
    return _CONTEXT_WINDOWS.get(provider_type, FALLBACK_CONTEXT_WINDOW)


__all__ = ["FALLBACK_CONTEXT_WINDOW", "default_context_window"]
