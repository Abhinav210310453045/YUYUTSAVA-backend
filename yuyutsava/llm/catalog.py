"""What is known about a model before it is called: its context window.

The window is what the context budget steers under — when compaction triggers,
what the context meter shows. For one model it is, first to last:

1. the alias's ``context_window`` in ``models.toml`` — the owner's word;
2. the model's own profile, as its SDK publishes it (LangChain's model profiles);
3. the usual window of its family — Claude's whether on Anthropic, Bedrock or
   Vertex, Gemini's, an open model's.

:func:`default_context_window` answers for a provider type alone, for the
environment route that names no model family (``LLM_PROVIDER``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yuyutsava.llm.target import ModelTarget

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


#: The usual window of each family Vertex serves, where it differs from the type's.
_VERTEX_FAMILY_WINDOWS: dict[str, int] = {
    "gemini": 1_000_000,
    "anthropic": 200_000,
    "maas": 128_000,
}


def default_context_window(provider_type: str) -> int:
    """The input window to steer under for a model of *provider_type*."""
    return _CONTEXT_WINDOWS.get(provider_type, FALLBACK_CONTEXT_WINDOW)


def context_window(target: ModelTarget, profile: Mapping[str, Any] | None = None) -> int:
    """The input window for *target*'s model; *profile* is the built model's ``.profile``."""
    if target.entry is not None and target.entry.context_window:
        return target.entry.context_window
    window = (profile or {}).get("max_input_tokens")
    if isinstance(window, int) and window > 0:
        return window
    if target.type == "vertex":
        return _VERTEX_FAMILY_WINDOWS[target.family]
    return default_context_window(target.type)


__all__ = ["FALLBACK_CONTEXT_WINDOW", "context_window", "default_context_window"]
